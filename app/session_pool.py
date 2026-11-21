"""Quản lý ngăn phiên chat (Sticky Session Pool) và lưu/khôi phục phiên trên đĩa."""
from dataclasses import dataclass
import hashlib
import json
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from app.config import LOCK_ACQUIRE_TIMEOUT, logger
from app.handoff import FactLedger

SESSION_POOL_MAX_SIZE = 16
SESSION_POOL_TTL = 3600.0
_AGENT_NAME_RE = re.compile(r'Your name is\s+"([^"]+)"', re.IGNORECASE)


def _gs():
    return sys.modules["app.gemini_session"]


@dataclass
class SessionSlot:
    """Ngăn phiên chat độc lập cho từng hội thoại/subagent (cô lập cid/rid/rcid và model)."""

    session_key: str
    chat_session: Any = None
    session_initialized: bool = False
    turn_count: int = 0
    tool_header_sent: bool = False
    chat_tool_header_hash: str = ""
    last_used_ts: float = 0.0
    model: str = "unspecified"
    ledger: Any = None
    is_subagent: bool = False
    workspace_root: str = ""


def _message_text_for_session_key(content: Any) -> Tuple[str, bool]:
    if isinstance(content, str):
        return content, False
    if isinstance(content, list):
        texts = []
        has_image = False
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                texts.append(str(part.get("text", "")))
            elif part.get("type") in ("image_url", "image"):
                has_image = True
        return "\n".join(texts), has_image
    return str(content or ""), False


def is_subagent_messages(messages: List[Dict[str, Any]]) -> bool:
    """Trả True nếu lời nhắc đánh dấu hội thoại thuộc về một subagent riêng."""
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        raw_text, _ = _message_text_for_session_key(msg.get("content"))
        if raw_text:
            match = _AGENT_NAME_RE.search(raw_text)
            if match:
                name = match.group(1).strip().lower()
                if name and name not in ("lead", "main", "default"):
                    return True
    return False


def compute_session_key(
    messages: List[Dict[str, Any]], explicit_session_id: str = ""
) -> Tuple[str, bool]:
    """Tính khóa bám phiên (Sticky Routing) tách Lead và từng Subagent."""
    from app.prompt_builder import _strip_injected_boilerplate

    msg_list = [m for m in (messages or []) if isinstance(m, dict)]
    is_new_conversation = not any(
        m.get("role") in ("assistant", "tool") for m in msg_list
    )

    clean_explicit = str(explicit_session_id or "").strip()
    if clean_explicit:
        return clean_explicit[:64], is_new_conversation

    agent_name = ""
    first_user = ""
    for msg in msg_list:
        raw_text, has_image = _message_text_for_session_key(msg.get("content"))
        if raw_text:
            match = _AGENT_NAME_RE.search(raw_text)
            if match:
                agent_name = match.group(1).strip().lower()
        if not first_user and str(msg.get("role", "")).lower() in ("user", "human"):
            cleaned = _strip_injected_boilerplate(raw_text)
            if cleaned:
                first_user = " ".join(cleaned.split())[:240]
            elif has_image:
                first_user = "[image]"

    if not agent_name and not first_user:
        return "default", is_new_conversation

    material = f"{agent_name}\x00{first_user}".encode("utf-8", errors="replace")
    return hashlib.sha256(material).hexdigest()[:16], is_new_conversation


def load_saved_session() -> Optional[dict]:
    """Đọc metadata phiên chat đã lưu (nếu có)."""
    state_file = _gs().SESSION_STATE_FILE
    try:
        if state_file.exists():
            data = json.loads(state_file.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("metadata"), list):
                return data
    except Exception as e:
        logger.warning(f"Không đọc được session state: {e}")
    return None


def save_session_state(
    metadata: list,
    model: str,
    account_key: str = "",
    tool_header_hash: str = "",
) -> None:
    """Lưu metadata phiên chat để resume sau khi restart."""
    state_file = _gs().SESSION_STATE_FILE
    try:
        state_file.write_text(
            json.dumps(
                {
                    "metadata": list(metadata),
                    "model": model,
                    "account_key": account_key,
                    "tool_header_hash": tool_header_hash,
                    "saved_at": time.time(),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning(f"Không lưu được session state: {e}")


def clear_saved_session() -> None:
    """Xóa session state đã lưu (dùng khi reset session / đổi account)."""
    state_file = _gs().SESSION_STATE_FILE
    try:
        if state_file.exists():
            state_file.unlink()
    except Exception:
        pass


def _tool_header_fingerprint(header: str) -> str:
    if not header:
        return ""
    return hashlib.sha256(header.encode("utf-8")).hexdigest()


class SessionPoolMixin:
    """Mixin quản lý Sticky Session Pool, model độc lập theo slot, metadata và lưu/khôi phục ChatSession."""

    def _current_slot_model(self) -> str:
        slot_model = str(getattr(self, "_active_slot_model", "") or "").strip()
        if slot_model:
            return slot_model
        return str(getattr(self, "default_model", "unspecified") or "unspecified").strip() or "unspecified"

    def _create_new_chat_session(
        self,
        preserve_metadata: Optional[list] = None,
        model_override: Optional[str] = None,
    ) -> None:
        kwargs = {}
        target_model = (
            str(model_override).strip()
            if model_override is not None
            else self._current_slot_model()
        )
        if not target_model:
            target_model = "unspecified"
        if target_model != "unspecified":
            kwargs["model"] = target_model
        if preserve_metadata:
            kwargs["metadata"] = preserve_metadata

        self.chat_session = self.client.start_chat(**kwargs)
        self._active_slot_model = target_model
        if not preserve_metadata:
            self.session_initialized = False
            self.turn_count = 0
            self.tool_header_sent = False
            self.chat_tool_header_hash = ""

    def _ensure_session_pool(self) -> Dict[str, SessionSlot]:
        pool = getattr(self, "_session_pool", None)
        if pool is None:
            pool = {}
            self._session_pool = pool
        if not getattr(self, "_active_session_key", None):
            self._active_session_key = "default"
        return pool

    def _snapshot_current_slot(self, session_key: str) -> SessionSlot:
        return SessionSlot(
            session_key=session_key,
            chat_session=self.chat_session,
            session_initialized=bool(self.session_initialized),
            turn_count=int(self.turn_count),
            tool_header_sent=bool(self.tool_header_sent),
            chat_tool_header_hash=str(getattr(self, "chat_tool_header_hash", "") or ""),
            last_used_ts=time.time(),
            model=self._current_slot_model(),
            ledger=getattr(self, "ledger", None),
            is_subagent=bool(getattr(self, "_active_is_subagent", False)),
            workspace_root=str(getattr(self, "_active_workspace_root", "") or ""),
        )

    def _ensure_slot_ledger(self, session_key: str, is_subagent: bool) -> FactLedger:
        handoff_dir = _gs().INSTANCE_PATHS["handoff_dir"]
        if not is_subagent:
            lead_ledger = getattr(self, "_lead_ledger", None)
            if lead_ledger is None:
                cur_ledger = getattr(self, "ledger", None)
                if cur_ledger is not None and getattr(cur_ledger, "persist", True):
                    lead_ledger = cur_ledger
                else:
                    lead_ledger = FactLedger(handoff_dir, filename="ledger.json", persist=True)
                self._lead_ledger = lead_ledger
            return lead_ledger
        safe_key = re.sub(r"[^A-Za-z0-9_-]", "_", session_key or "subagent")[:32]
        return FactLedger(handoff_dir, filename=f"ledger_{safe_key}.json", persist=False)

    def _sync_active_session_slot(self) -> None:
        pool = self._ensure_session_pool()
        active_key = self._active_session_key
        pool[active_key] = self._snapshot_current_slot(active_key)

    def _evict_session_pool_locked(self, protected_keys: set) -> None:
        pool = self._ensure_session_pool()
        now = time.time()
        expired = [
            key
            for key, slot in pool.items()
            if key not in protected_keys and (now - slot.last_used_ts) > SESSION_POOL_TTL
        ]
        for key in expired:
            pool.pop(key, None)
        while len(pool) > SESSION_POOL_MAX_SIZE:
            candidates = [
                (slot.last_used_ts, key)
                for key, slot in pool.items()
                if key not in protected_keys
            ]
            if not candidates:
                break
            _, oldest_key = min(candidates)
            pool.pop(oldest_key, None)

    def ensure_slot_model_locked(
        self, target_model: Optional[str], keep_chat: bool = True
    ) -> str:
        """Đảm bảo CHỈ slot đang active dùng đúng `target_model` (không sửa `default_model` hay slot khác)."""
        desired = str(target_model or "").strip()
        if not desired or desired.lower() in ("default", "auto", "none", "gemini"):
            desired = self._current_slot_model()
        if not desired:
            desired = "unspecified"

        current_model = self._current_slot_model()
        chat_model_attr = getattr(self.chat_session, "model", None) if self.chat_session is not None else None
        if isinstance(chat_model_attr, str) and chat_model_attr.strip():
            current_chat_model = chat_model_attr.strip()
        elif self.chat_session is not None and current_model == "unspecified":
            current_chat_model = "unspecified"
        else:
            current_chat_model = current_model

        if (
            self.chat_session is not None
            and keep_chat
            and desired == current_model
            and desired == current_chat_model
        ):
            self._active_slot_model = desired
            pool = self._ensure_session_pool()
            if self._active_session_key in pool:
                pool[self._active_session_key].model = desired
            return desired

        saved_meta = (
            list(self.chat_session.metadata)
            if (keep_chat and self.chat_session is not None and hasattr(self.chat_session, "metadata"))
            else None
        )
        self._create_new_chat_session(preserve_metadata=saved_meta, model_override=desired)
        if saved_meta:
            self.tool_header_sent = False
            self.chat_tool_header_hash = ""
        self._sync_active_session_slot()
        logger.info(
            f"[SLOT-MODEL] Slot '{self._active_session_key[:12]}': [{current_model}] --> [{desired}]"
        )
        return desired

    def _activate_session_slot_locked(
        self,
        session_key: str,
        is_new_conversation: bool,
        is_subagent: bool = False,
        requested_model: Optional[str] = None,
        workspace_root: str = "",
    ) -> SessionSlot:
        target_key = str(session_key or "default").strip() or "default"
        pool = self._ensure_session_pool()
        active_key = self._active_session_key
        has_dirty_chat = bool(
            self.chat_session is not None and getattr(self.chat_session, "cid", None)
        )
        if active_key and (self.turn_count > 0 or has_dirty_chat or active_key in pool):
            pool[active_key] = self._snapshot_current_slot(active_key)

        self._evict_session_pool_locked({target_key, active_key})

        desired_new_model = (
            str(requested_model or "").strip()
            if requested_model and str(requested_model).strip().lower() not in ("default", "auto", "none", "gemini")
            else str(getattr(self, "default_model", "unspecified") or "unspecified").strip() or "unspecified"
        )

        if is_new_conversation:
            existing_target = pool.get(target_key)
            target_was_used = bool(
                existing_target is not None
                and (
                    existing_target.turn_count > 0
                    or (
                        existing_target.chat_session is not None
                        and getattr(existing_target.chat_session, "cid", None)
                    )
                )
            )
            other_active_slots = any(
                key != target_key
                and (
                    slot.turn_count > 0
                    or (slot.chat_session is not None and getattr(slot.chat_session, "cid", None))
                )
                for key, slot in pool.items()
            )
            if (
                target_was_used
                or self.turn_count > 0
                or has_dirty_chat
                or other_active_slots
                or self.chat_session is None
                or self._current_slot_model() != desired_new_model
            ):
                self._create_new_chat_session(preserve_metadata=None, model_override=desired_new_model)
            else:
                self._active_slot_model = desired_new_model
            self._active_is_subagent = bool(is_subagent)
            self._active_workspace_root = str(workspace_root or "").strip()
            self.ledger = self._ensure_slot_ledger(target_key, self._active_is_subagent)
            pool[target_key] = self._snapshot_current_slot(target_key)
        elif target_key == active_key:
            existing_slot = pool.get(target_key)
            slot_is_subagent = bool(
                is_subagent
                or (
                    existing_slot.is_subagent
                    if existing_slot
                    else getattr(self, "_active_is_subagent", False)
                )
            )
            self._active_is_subagent = slot_is_subagent
            if workspace_root:
                self._active_workspace_root = str(workspace_root).strip()
            elif existing_slot and existing_slot.workspace_root:
                self._active_workspace_root = existing_slot.workspace_root
            self.ledger = (
                existing_slot.ledger
                if existing_slot and existing_slot.ledger is not None
                else self._ensure_slot_ledger(target_key, slot_is_subagent)
            )
            if target_key in pool:
                pool[target_key].ledger = self.ledger
                pool[target_key].is_subagent = self._active_is_subagent
                pool[target_key].workspace_root = getattr(self, "_active_workspace_root", "")
                pool[target_key].last_used_ts = time.time()
            else:
                pool[target_key] = self._snapshot_current_slot(target_key)
        elif target_key in pool:
            slot = pool[target_key]
            self.chat_session = slot.chat_session
            self.session_initialized = slot.session_initialized
            self.turn_count = slot.turn_count
            self.tool_header_sent = slot.tool_header_sent
            self.chat_tool_header_hash = slot.chat_tool_header_hash
            self._active_slot_model = slot.model or self.default_model or "unspecified"
            self._active_workspace_root = str(workspace_root or slot.workspace_root or "").strip()
            slot.workspace_root = self._active_workspace_root
            slot.is_subagent = bool(is_subagent or slot.is_subagent)
            if slot.ledger is None:
                slot.ledger = self._ensure_slot_ledger(target_key, slot.is_subagent)
            self._active_is_subagent = slot.is_subagent
            self.ledger = slot.ledger
            slot.last_used_ts = time.time()
        else:
            if (
                self.turn_count > 0
                or has_dirty_chat
                or self.chat_session is None
                or self._current_slot_model() != desired_new_model
            ):
                self._create_new_chat_session(preserve_metadata=None, model_override=desired_new_model)
            else:
                self._active_slot_model = desired_new_model
            self._active_is_subagent = bool(is_subagent)
            self._active_workspace_root = str(workspace_root or "").strip()
            self.ledger = self._ensure_slot_ledger(target_key, self._active_is_subagent)
            pool[target_key] = self._snapshot_current_slot(target_key)

        self._active_session_key = target_key
        if requested_model:
            self.ensure_slot_model_locked(requested_model, keep_chat=True)
        self._evict_session_pool_locked({target_key})
        return pool[target_key]

    async def activate_session_slot(
        self,
        session_key: str,
        is_new_conversation: bool,
        is_subagent: bool = False,
        requested_model: Optional[str] = None,
        workspace_root: str = "",
    ) -> SessionSlot:
        acquired_here = await self.acquire_turn_lock(LOCK_ACQUIRE_TIMEOUT)
        try:
            return self._activate_session_slot_locked(
                session_key,
                is_new_conversation,
                is_subagent=is_subagent,
                requested_model=requested_model,
                workspace_root=workspace_root,
            )
        finally:
            self.release_turn_lock(acquired_here)

    def _chat_metadata_snapshot(self):
        try:
            meta = list(self.chat_session.metadata) if self.chat_session is not None else None
        except Exception:
            return None
        return meta if meta and meta[0] else None

    def current_chat_key(self) -> str:
        try:
            cid = self.chat_session.metadata[0] if self.chat_session else None
        except Exception:
            cid = None
        return hashlib.sha256(cid.encode("utf-8")).hexdigest()[:8] if cid else "new"

    def _sanitize_chat_metadata_before_send(self) -> Optional[list]:
        saved_meta = self._chat_metadata_snapshot()
        if not saved_meta:
            return None
        has_cid = bool(saved_meta[0]) if len(saved_meta) >= 1 else False
        has_rcid = bool(saved_meta[2]) if len(saved_meta) >= 3 else False
        if has_cid and (not has_rcid or (self.turn_count == 0 and not self.session_initialized)):
            logger.warning(
                "[SESSION] Phát hiện metadata dở dang (cid có nhưng chưa hoàn tất lượt) -> Làm mới ChatSession để tránh lỗi 1097."
            )
            self._create_new_chat_session(preserve_metadata=None)
            return None
        return saved_meta

    def _restore_chat_metadata_after_abort(
        self,
        saved_meta: Optional[list],
        model_override: Optional[str] = None,
    ) -> None:
        has_valid_saved = bool(
            saved_meta and len(saved_meta) >= 3 and saved_meta[0] and saved_meta[2]
        )
        if has_valid_saved:
            self._create_new_chat_session(
                preserve_metadata=list(saved_meta), model_override=model_override
            )
        else:
            self._create_new_chat_session(
                preserve_metadata=None, model_override=model_override
            )
        self._sync_active_session_slot()

    def _resume_saved_session(self, saved: dict) -> bool:
        try:
            meta = saved.get("metadata")
            if not isinstance(meta, list) or not meta or not meta[0]:
                return False
            saved_model = saved.get("model")
            if saved_model and saved_model != "unspecified":
                self.default_model = str(saved_model)
            self._ensure_default_model()
            self._active_slot_model = self.default_model
            self._create_new_chat_session(preserve_metadata=list(meta), model_override=self.default_model)
            saved_header_hash = saved.get("tool_header_hash")
            self.chat_tool_header_hash = (
                saved_header_hash if isinstance(saved_header_hash, str) else ""
            )
            self.session_initialized = True
            self.tool_header_sent = bool(self.chat_tool_header_hash)
            self.turn_count = 0
            self._sync_active_session_slot()
            logger.info(f"Đã khôi phục phiên chat cũ (cid={meta[0]}).")
            return True
        except Exception as e:
            logger.warning(f"Không khôi phục được phiên chat cũ: {e}")
            return False

    def _save_session(self) -> None:
        if not self.auth_ok or self.chat_session is None:
            return
        try:
            meta = list(self.chat_session.metadata)
        except Exception:
            return
        if meta and meta[0]:
            save_session_state(
                meta,
                self.default_model,
                self.account_key,
                tool_header_hash=(
                    self.chat_tool_header_hash if self.tool_header_sent else ""
                ),
            )

    async def reset_session(self) -> None:
        acquired_here = await self.acquire_turn_lock(LOCK_ACQUIRE_TIMEOUT)
        try:
            self._active_slot_model = self.default_model
            self._active_workspace_root = ""
            self._create_new_chat_session(model_override=self.default_model)
            self._ensure_session_pool().clear()
            self._active_session_key = "default"
            self._active_is_subagent = False
            self.ledger = self._ensure_slot_ledger("default", False)
            self._sync_active_session_slot()
            clear_saved_session()
            logger.info(f"Đã làm mới phiên ChatSession (Model: {self.default_model}).")
        finally:
            self.release_turn_lock(acquired_here)
