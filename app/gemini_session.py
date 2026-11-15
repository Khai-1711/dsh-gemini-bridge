import asyncio
from datetime import datetime
import hashlib
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from gemini_webapi import GeminiClient
from gemini_webapi.constants import AccountStatus
from gemini_webapi.exceptions import AuthError, UsageLimitExceededError

from app.accounts import (
    account_key,
    first_available_index,
    load_state,
    mark_exhausted,
    next_available_index,
    save_state,
    write_chain_file,
    find_chain_overlap,
)
from app.config import (
    ACCOUNT_CHAIN,
    AUTH_COOLDOWN_MINUTES,
    BACKUP_1PSID,
    BACKUP_1PSIDTS,
    BASE_DIR,
    BRIDGE_DEBUG_FAKE_QUOTA,
    DISABLE_DRAIN_TIMEOUT,
    ENABLE_RESPONSE_CACHE,
    GEMINI_HARD_TIMEOUT,
    HANDOFF_ENABLE,
    HANDOFF_FLASH_TIMEOUT,
    HANDOFF_MAX_CHARS,
    HANDOFF_RECENT_TURNS,
    HANDOFF_SOURCE_MAX_CHARS,
    INSTANCE_ID,
    INSTANCE_LABEL,
    INSTANCE_PATHS,
    LOCK_ACQUIRE_TIMEOUT,
    MIN_REQUEST_INTERVAL,
    PORT,
    QUOTA_COOLDOWN_HOURS,
    REPLAY_MAX_CHARS,
    SECURE_1PSID,
    SECURE_1PSIDTS,
    logger,
)
from app.errors import (
    BridgeAuthError,
    BridgeBusyError,
    BridgeControlError,
    BridgeDisabledError,
    BridgeError,
    BridgeQuotaError,
    BridgeTimeoutError,
    BridgeUpstreamError,
)
from app.handoff import (
    FactLedger,
    SECTION_ORDER,
    bound_sections,
    build_handoff_message,
    parse_sections,
)
from app.vision import cleanup_temp_images, extract_text_from_image_local

# Python 3.11+ has asyncio.timeout; keep 3.10 compatibility with a no-op fallback.
try:
    from asyncio import timeout as _asyncio_timeout
except ImportError:  # pragma: no cover
    from contextlib import nullcontext

    def _asyncio_timeout(_seconds):
        return nullcontext()


# =====================================================================
# RESPONSE CACHE (TTL-based, tránh gọi lại prompt lặp)
# =====================================================================
_response_cache: Dict[str, Tuple[float, str]] = {}
CACHE_TTL = 300  # 5 phút
MAX_CACHE_SIZE = 50


def _cache_key(prompt: str, model: str = "") -> str:
    material = f"{model}\x00{prompt}".encode("utf-8", errors="replace")
    return hashlib.sha256(material).hexdigest()[:16]


def get_cached_response(prompt: str, model: str = "") -> Optional[str]:
    """Tìm response đã cache cho prompt (theo hash, gồm cả model)."""
    key = _cache_key(prompt, model)
    if key in _response_cache:
        ts, resp = _response_cache[key]
        if time.time() - ts < CACHE_TTL:
            logger.info(f"[CACHE HIT] hash={key}")
            return resp
        del _response_cache[key]
    return None


def set_cached_response(prompt: str, response: str, model: str = "") -> None:
    """Lưu response vào cache. Tự xóa entry cũ nhất khi đầy."""
    if len(_response_cache) >= MAX_CACHE_SIZE:
        oldest_key = min(_response_cache, key=lambda k: _response_cache[k][0])
        del _response_cache[oldest_key]
    _response_cache[_cache_key(prompt, model)] = (time.time(), response)


def clear_response_cache() -> int:
    """Xóa toàn bộ cache. Trả về số entry đã xóa."""
    count = len(_response_cache)
    _response_cache.clear()
    return count


# =====================================================================
# SESSION PERSISTENCE (giữ nguyên 1 đoạn chat qua các lần restart)
# =====================================================================
SESSION_STATE_FILE = INSTANCE_PATHS["session_file"]


def load_saved_session() -> Optional[dict]:
    """Đọc metadata phiên chat đã lưu (nếu có)."""
    try:
        if SESSION_STATE_FILE.exists():
            data = json.loads(SESSION_STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("metadata"), list):
                return data
    except Exception as e:
        logger.warning(f"Không đọc được session state: {e}")
    return None


def save_session_state(metadata: list, model: str, account_key: str = "") -> None:
    """Lưu metadata phiên chat để resume sau khi restart."""
    try:
        SESSION_STATE_FILE.write_text(
            json.dumps(
                {
                    "metadata": [m for m in metadata],
                    "model": model,
                    "account_key": account_key,
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
    try:
        if SESSION_STATE_FILE.exists():
            SESSION_STATE_FILE.unlink()
    except Exception:
        pass


# =====================================================================
# SINGLE CHAT MANAGER (Stateful Session + Failover + Cache)
# =====================================================================
class SingleChatManager:
    """
    Quản lý phiên hội thoại đơn nhất với Gemini Web:
    - asyncio.Lock() với timeout (trả 429 thay vì chờ vô hạn)
    - Cookie failover tự động sang tài khoản dự phòng
    - Theo dõi auth errors liên tiếp → cảnh báo cookie hết hạn
    - Response cache cho prompt lặp
    - auto_refresh=True để giữ session sống lâu
    """

    def __init__(self) -> None:
        self.chain = ACCOUNT_CHAIN
        self.account_state = load_state(INSTANCE_PATHS["accounts_state_file"])
        self.active_index = first_available_index(
            self.chain, self.account_state, time.time()
        )
        if self.active_index is None:
            self.active_index = 0
            logger.warning(
                "[QUOTA] Tất cả tài khoản đang trong thời gian nghỉ; dùng tài khoản chính."
            )

        initial_acc = (
            self.chain[self.active_index]
            if (self.chain and 0 <= self.active_index < len(self.chain))
            else {
                "label": "chinh",
                "psid": SECURE_1PSID,
                "psidts": SECURE_1PSIDTS,
                "key": account_key(SECURE_1PSID),
            }
        )
        self.client = GeminiClient(initial_acc["psid"], initial_acc["psidts"])
        self.lock = asyncio.Lock()
        self.state: str = "off"
        self.enabled_since: Optional[float] = None
        self._control_lock = asyncio.Lock()
        self._inflight: int = 0
        self._idle_event = asyncio.Event()
        self._idle_event.set()
        self.available_models: List[str] = ["unspecified"]
        self.model_details: List[Dict[str, Any]] = []
        self.default_model: str = "unspecified"
        self.tool_header_sent = False
        self.chat_session = None
        self.session_initialized = False
        self.last_request_ts = 0.0
        self.turn_count = 0
        self.total_requests = 0
        self.total_errors = 0
        self.start_time = time.time()
        self._consecutive_auth_errors = 0
        self._using_backup = self.active_index > 0
        self.auth_ok: bool = False
        self.account_status_name: str = "UNKNOWN"
        self.cookie_source: str = ""
        self._active_psid: str = initial_acc["psid"]
        self._last_recover_ts: float = 0.0
        self.last_tool_header: str = ""
        self.vision_disabled_until: float = 0.0
        self._consecutive_transient_errors: int = 0

        # T24b: Ghi chain file và kiểm tra trùng lặp giữa các instance
        try:
            write_chain_file(
                INSTANCE_PATHS["chain_file"],
                INSTANCE_ID,
                PORT,
                [a["key"] for a in self.chain],
                time.time(),
            )
            self.chain_overlap = find_chain_overlap(
                BASE_DIR,
                INSTANCE_PATHS["chain_file"],
                [a["key"] for a in self.chain],
            )
            for x in self.chain_overlap:
                logger.critical(
                    f"[ACCOUNTS] Một tài khoản trong chuỗi của con '{INSTANCE_ID or 'default'}' "
                    f"cũng nằm trong chuỗi của con '{x.get('other_instance')}'. Hai con sẽ dùng chung một tài khoản."
                )
        except Exception as exc:
            self.chain_overlap = []
            logger.warning(f"[ACCOUNTS] Lỗi ghi chain file: {exc}")

        # T25: FactLedger & Transcript
        self.ledger = FactLedger(INSTANCE_PATHS["handoff_dir"])
        self.transcript: list = []
        self._pending_handoff = None
        self._fake_quota_done = False
        self.on_event = None

    HEADER_MARKER = "=== OPERATOR INSTRUCTIONS"

    @property
    def vision_upload_available(self) -> bool:
        return time.time() >= self.vision_disabled_until

    def _is_auth_error(self, exc: BaseException) -> bool:
        if isinstance(exc, AuthError):
            return True
        text = str(exc).lower()
        if any(k in text for k in ("permission denied", "unauthenticated", "cookie")):
            return True
        status = getattr(self.client, "account_status", None)
        source = str(getattr(self.client, "_cookie_source", "") or "").lower()
        return status != AccountStatus.AVAILABLE or source == "guest"

    @property
    def account_key(self) -> str:
        """Hash rút gọn của PSID đang dùng - KHÔNG lộ cookie."""
        return hashlib.sha256(self._active_psid.encode("utf-8")).hexdigest()[:12]

    def _evaluate_auth(self) -> bool:
        """Đọc dấu hiệu đăng nhập từ client (không gọi mạng) và cập nhật cờ.

        gemini-webapi 2.x có thể âm thầm rơi sang phiên Guest mà init() vẫn
        thành công. Dấu hiệu: account_status != AVAILABLE hoặc
        _cookie_source == "Guest".
        """
        status = getattr(self.client, "account_status", None)
        source = str(getattr(self.client, "_cookie_source", "") or "")
        self.cookie_source = source
        self.account_status_name = getattr(status, "name", str(status))
        self.auth_ok = (
            status == AccountStatus.AVAILABLE and source.lower() != "guest"
        )
        if self.auth_ok:
            logger.info(
                f"[AUTH] Đã đăng nhập OK (nguồn cookie: {source or 'n/a'})."
            )
        else:
            logger.critical(
                f"[AUTH] KHÔNG ĐĂNG NHẬP! Trạng thái={self.account_status_name}, "
                f"nguồn cookie={source or 'n/a'}. Đang ở GUEST MODE hoặc cookie hết hạn "
                "-> không dùng được Gemini Pro. Cập nhật cookie ở Dashboard "
                "hoặc POST /v1/update-cookies."
            )
        return self.auth_ok

    def _refresh_auth_flags_quietly(self) -> None:
        """Cập nhật auth_ok từ client mà KHÔNG ghi log CRITICAL mỗi request."""
        status = getattr(self.client, "account_status", None)
        source = str(getattr(self.client, "_cookie_source", "") or "")
        self.cookie_source = source
        self.account_status_name = getattr(status, "name", str(status))
        self.auth_ok = (
            status == AccountStatus.AVAILABLE and source.lower() != "guest"
        )

    def _chat_metadata_snapshot(self):
        """Chụp metadata (cid, rid, rcid) của đoạn chat hiện tại để giữ lại khi tạo lại session."""
        try:
            meta = list(self.chat_session.metadata) if self.chat_session is not None else None
        except Exception:
            return None
        return meta if meta and meta[0] else None

    def current_chat_key(self) -> str:
        """Mã rút gọn của đoạn chat hiện tại (hash của cid). Đổi giá trị = đã sang chat khác."""
        try:
            cid = self.chat_session.metadata[0] if self.chat_session else None
        except Exception:
            cid = None
        return hashlib.sha256(cid.encode("utf-8")).hexdigest()[:8] if cid else "new"

    async def ensure_authenticated(self) -> bool:
        """Trả True nếu đang đăng nhập. Nếu không, thử phục hồi tối đa 1 lần/60 giây."""
        if self.state == "off":
            return False
        self._refresh_auth_flags_quietly()
        if self.auth_ok:
            return True

        now = time.time()
        if now - self._last_recover_ts < 60:
            return False
        self._last_recover_ts = now

        async with self.lock:
            logger.warning("[AUTH] Đang thử phục hồi đăng nhập (init lại client)...")
            try:
                try:
                    await self.client.close()
                except Exception:
                    pass
                await self.client.init(
                    timeout=60, auto_close=False, close_delay=300, auto_refresh=True
                )
                self._refresh_available_models()
                self._ensure_default_model()
            except Exception as exc:
                logger.error(f"[AUTH] Init lại thất bại: {exc}")
                return False
            if self._evaluate_auth():
                saved = self._chat_metadata_snapshot()
                self._create_new_chat_session(preserve_metadata=saved)
                return True
            if await self._try_failover_to_backup():
                return True
            return False

    def _emit(self, **fields) -> None:
        if self.on_event:
            try:
                self.on_event(**fields)
            except Exception:
                pass

    def remember_transcript(self, messages) -> None:
        """Lưu bản ghi GỌN của hội thoại (DSH gửi lại toàn bộ messages ở mỗi request)."""
        lines = []
        for m in messages:
            role = m.get("role")
            if role == "system":
                continue
            content = m.get("content")
            if isinstance(content, list):
                text = " ".join(
                    p.get("text", "")
                    for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                )
                if any(
                    isinstance(p, dict) and p.get("type") == "image_url"
                    for p in content
                ):
                    text += " [ảnh]"
            else:
                text = str(content or "")
            if role == "assistant" and m.get("tool_calls"):
                calls = []
                for tc in m["tool_calls"]:
                    fn = tc.get("function", {})
                    calls.append(
                        f'{fn.get("name", "?")}({str(fn.get("arguments", ""))[:200]})'
                    )
                text = text + " [tool_calls: " + "; ".join(calls) + "]"
            limit = 600 if role == "tool" else 1200
            lines.append(f"{role}: " + " ".join(text.split())[:limit])
        self.transcript = lines[-60:]

    def remember_tool_header(self, header: str) -> None:
        if header:
            self.last_tool_header = header

    def _ensure_header_in_prompt(self, prompt: str) -> str:
        """Chat hiện tại chưa nhận tool header mà prompt chưa có -> nối vào cuối."""
        if (
            self.needs_tool_header
            and self.last_tool_header
            and self.HEADER_MARKER not in prompt
        ):
            return (
                prompt
                + "\n\n=== OPERATOR INSTRUCTIONS (apply to every reply in this chat) ===\n"
                + self.last_tool_header
            )
        return prompt

    def _recent_turns_for_handoff(self) -> list:
        convo = [
            l
            for l in self.transcript
            if l.startswith(("user:", "assistant:"))
        ]
        return convo[-(2 * HANDOFF_RECENT_TURNS) :]

    def _apply_pending_handoff(self, prompt: str) -> str:
        """CHỈ DỰNG CHUỖI. KHÔNG được xóa self._pending_handoff ở đây."""
        if not self._pending_handoff:
            return prompt
        bounded, archive = self._pending_handoff
        msg = build_handoff_message(
            bounded, self._recent_turns_for_handoff(), archive, REPLAY_MAX_CHARS
        )
        logger.info(f"[QUOTA] Tin bàn giao gửi cho tài khoản mới: {len(msg)} ký tự.")
        return msg + "\n" + prompt

    def require_on(self) -> None:
        """Ném BridgeDisabledError nếu bridge chưa BẬT. Gọi ở đầu mọi đường gửi tin."""
        if self.state == "on":
            return
        msgs = {
            "off": "Bridge đang TẮT. Hãy bật ở dashboard rồi thử lại.",
            "starting": "Bridge đang khởi động, thử lại sau ít giây.",
            "stopping": "Bridge đang tắt.",
        }
        raise BridgeDisabledError(msgs.get(self.state, "Bridge chưa sẵn sàng."))

    async def enable(self) -> None:
        if self._control_lock.locked():
            raise BridgeControlError("Đang có thao tác bật/tắt khác, thử lại sau.")
        async with self._control_lock:
            if self.state == "on":
                return
            self.state = "starting"
            logger.info("[CONTROL] Đang BẬT bridge...")
            try:
                await self.initialize()
            except Exception as exc:
                logger.error(f"[CONTROL] Bật thất bại: {exc}")
                try:
                    await self.client.close()
                except Exception:
                    pass
                self.state = "off"
                raise BridgeUpstreamError("Không bật được: không kết nối được Gemini Web.")
            self.state = "on"
            self.enabled_since = time.time()
            logger.info("[CONTROL] Bridge đã BẬT.")
            self._emit(event="control_enable")

    async def disable(self) -> None:
        if self._control_lock.locked():
            raise BridgeControlError("Đang có thao tác bật/tắt khác, thử lại sau.")
        async with self._control_lock:
            if self.state == "off":
                return
            self.state = "stopping"
            logger.info("[CONTROL] Đang TẮT bridge, chờ request đang chạy...")
            try:
                await asyncio.wait_for(self._idle_event.wait(), timeout=DISABLE_DRAIN_TIMEOUT)
            except asyncio.TimeoutError:
                self.state = "on"
                logger.warning("[CONTROL] Còn request đang chạy; hủy việc tắt.")
                raise BridgeControlError(
                    f"Còn request đang chạy quá {int(DISABLE_DRAIN_TIMEOUT)}s nên chưa tắt được. "
                    "Hãy thử lại sau."
                )
            self._save_session()
            try:
                await self.client.close()
            except Exception as exc:
                logger.warning(f"[CONTROL] Đóng client có lỗi (bỏ qua): {exc}")
            self.state = "off"
            self.enabled_since = None
            logger.info("[CONTROL] Bridge đã TẮT.")
            self._emit(event="control_disable")

    async def initialize(self) -> None:
        logger.info("Đang khởi tạo Gemini WebClient...")
        await self.client.init(
            timeout=90, auto_close=False, close_delay=300, auto_refresh=True
        )

        # Quét danh sách model thực tế của tài khoản
        self._refresh_available_models()
        self._ensure_default_model()
        logger.info(
            f"Model khả dụng ({len(self.available_models)}): "
            f"{self.available_models}"
        )

        self._consecutive_auth_errors = 0
        if not self._evaluate_auth():
            # Thử tài khoản dự phòng; nếu vẫn lỗi thì server VẪN chạy (auth_ok=False)
            # để người dùng còn vào Dashboard cập nhật cookie. KHÔNG sys.exit.
            await self._try_failover_to_backup()

        if not self.auth_ok:
            self._create_new_chat_session()
            logger.info(f"SingleChatManager sẵn sàng (chưa xác thực)! Model: [{self.default_model}]")
            return

        saved = load_saved_session() if self.auth_ok else None
        if saved is not None and saved.get("account_key") != self.account_key:
            logger.warning("Bỏ qua .bridge_session.json: thuộc tài khoản khác.")
            saved = None
        if saved is not None and self._resume_saved_session(saved):
            logger.info(
                f"SingleChatManager sẵn sàng! (đã khôi phục phiên chat cũ) "
                f"Model: [{self.default_model}]"
            )
        else:
            self._create_new_chat_session()
            logger.info(f"SingleChatManager sẵn sàng! Model: [{self.default_model}]")

    def _refresh_available_models(self) -> List[str]:
        """Đọc danh sách model thật mà tài khoản Gemini đang có.

        ``gemini-webapi`` >= 2.x chỉ công bố model qua ``client.list_models()``
        (registry nội bộ được nạp lúc ``init()``); các thuộc tính thô như
        ``client.models`` / ``client.available_models`` không còn tồn tại.
        Vẫn giữ nhánh quét thuộc tính cũ làm fallback cho bản thư viện cũ hơn.
        """
        details: List[Dict[str, Any]] = []

        def _add(name: Any, display_name: Any = None, description: Any = None) -> None:
            clean = str(name or "").strip()
            if not clean or any(d["name"] == clean for d in details):
                return
            details.append(
                {
                    "name": clean,
                    "display_name": str(display_name or clean).strip() or clean,
                    "description": str(description or "").strip(),
                }
            )

        # Đường chuẩn: gemini-webapi 2.x
        try:
            for model in self.client.list_models() or []:
                _add(
                    getattr(model, "model_name", None)
                    or getattr(model, "name", None)
                    or model,
                    getattr(model, "display_name", None),
                    getattr(model, "description", None),
                )
        except Exception as e:
            logger.warning(f"Không đọc được model registry: {e}")

        # Fallback: bản thư viện cũ chỉ có thuộc tính thô
        if not details:
            for attr in ("models", "available_models", "_models"):
                raw_list = getattr(self.client, attr, None)
                if isinstance(raw_list, dict):
                    for key, value in raw_list.items():
                        _add(
                            getattr(value, "model_name", None) or key,
                            getattr(value, "display_name", None),
                            getattr(value, "description", None),
                        )
                elif isinstance(raw_list, (list, tuple, set)):
                    for item in raw_list:
                        _add(
                            getattr(item, "model_name", None)
                            or getattr(item, "name", None)
                            or item,
                            getattr(item, "display_name", None),
                            getattr(item, "description", None),
                        )
                if details:
                    break

        if not details:
            _add("unspecified")

        self.model_details = details
        self.available_models = [d["name"] for d in details]
        return self.available_models

    def _ensure_default_model(self) -> None:
        """Giữ model đang chọn nếu tài khoản còn model đó, ngược lại chọn model đầu."""
        selectable = [m for m in self.available_models if m != "unspecified"]
        current = str(self.default_model or "").strip()
        if current and current != "unspecified" and current in selectable:
            return
        self.default_model = selectable[0] if selectable else "unspecified"

    @property
    def needs_tool_header(self) -> bool:
        """True khi chat Gemini hiện tại chưa chắc đã nhận danh sách tool.

        Resume phiên cũ hoặc tạo chat mới đều khiến model mất ngữ cảnh tool
        nếu không gửi lại header -> model Gemini sẽ không biết gọi Read/Glob/
        Grep/Pwsh và chỉ hỏi người dùng đường dẫn.
        """
        return (not self.session_initialized) or (not self.tool_header_sent)

    def _create_new_chat_session(self, preserve_metadata: Optional[list] = None) -> None:
        kwargs = {}
        if self.default_model and self.default_model != "unspecified":
            kwargs["model"] = str(self.default_model)
        if preserve_metadata:
            kwargs["metadata"] = preserve_metadata

        self.chat_session = self.client.start_chat(**kwargs)
        if not preserve_metadata:
            self.session_initialized = False
            self.turn_count = 0
            self.tool_header_sent = False

    def _resume_saved_session(self, saved: dict) -> bool:
        """Khôi phục phiên chat cũ từ metadata đã lưu."""
        try:
            meta = saved.get("metadata")
            if not isinstance(meta, list) or not meta or not meta[0]:
                return False
            saved_model = saved.get("model")
            if saved_model and saved_model != "unspecified":
                self.default_model = str(saved_model)
            # Tài khoản có thể đã đổi danh sách model -> xác thực lại
            self._ensure_default_model()
            self._create_new_chat_session(preserve_metadata=list(meta))
            # Phiên cũ đã có context hội thoại, nhưng KHÔNG chắc còn danh sách tool
            # trong ngữ cảnh -> phải gửi lại header ở lượt kế tiếp, nếu không model
            # Gemini sẽ không gọi được Read/Glob/Grep/Pwsh.
            self.session_initialized = True
            self.tool_header_sent = False
            self.turn_count = 0
            logger.info(f"Đã khôi phục phiên chat cũ (cid={meta[0]}).")
            return True
        except Exception as e:
            logger.warning(f"Không khôi phục được phiên chat cũ: {e}")
            return False

    def _save_session(self) -> None:
        """Lưu metadata phiên hiện tại để resume sau khi restart."""
        if not self.auth_ok or self.chat_session is None:
            return
        try:
            meta = list(self.chat_session.metadata)
        except Exception:
            return
        if meta and meta[0]:
            save_session_state(meta, self.default_model, self.account_key)

    async def switch_model(self, new_model: str, keep_chat: bool = True) -> str:
        """Đổi model Gemini đang sử dụng."""
        async with self.lock:
            old_model = self.default_model
            target = str(new_model).strip()
            if target and target != "unspecified":
                try:
                    resolved = self.client.resolve_model(target)
                    target = str(getattr(resolved, "model_name", None) or target)
                except Exception:
                    if self.available_models and target not in self.available_models:
                        raise ValueError(
                            f"Model '{target}' không có trong tài khoản. "
                            f"Model khả dụng: {', '.join(self.available_models)}"
                        )
            self.default_model = target
            saved_meta = (
                list(self.chat_session.metadata)
                if (keep_chat and self.chat_session and hasattr(self.chat_session, "metadata"))
                else None
            )
            self._create_new_chat_session(preserve_metadata=saved_meta)
            self.tool_header_sent = False
            self._save_session()
            logger.info(f"==> CHUYỂN MODEL: [{old_model}] --> [{self.default_model}]")
            return self.default_model

    async def update_cookies(self, new_psid: str, new_psidts: str) -> dict:
        """Hot-reload cookie mà không cần restart server."""
        if self.state in ("starting", "stopping"):
            raise BridgeControlError("Đang có thao tác bật/tắt, không thể cập nhật cookie lúc này.")

        async with self.lock:
            if self.state == "off":
                self._active_psid = new_psid
                self.client = GeminiClient(new_psid, new_psidts)
                if self.chain and len(self.chain) > 0:
                    self.chain[0]["psid"] = new_psid
                    self.chain[0]["psidts"] = new_psidts
                    self.chain[0]["key"] = account_key(new_psid)
                logger.info("[CONTROL] Đã lưu cookie mới; sẽ dùng khi bật bridge.")
                return {"status": "saved_while_off"}

            try:
                await self.client.close()
            except Exception:
                pass
            self.client = GeminiClient(new_psid, new_psidts)
            await self.client.init(
                timeout=90, auto_close=False, close_delay=300, auto_refresh=True
            )
            self._active_psid = new_psid
            if self.chain and len(self.chain) > 0:
                self.chain[0]["psid"] = new_psid
                self.chain[0]["psidts"] = new_psidts
                self.chain[0]["key"] = account_key(new_psid)
            ok = self._evaluate_auth()
            self._refresh_available_models()
            self._ensure_default_model()
            self._create_new_chat_session()
            clear_saved_session()
            self.vision_disabled_until = 0.0
            self._consecutive_auth_errors = 0
            self._using_backup = False
            if not ok:
                raise AuthError("Cookie mới không đăng nhập được (guest/hết hạn)")
            logger.info("✅ Cookie đã được cập nhật thành công!")
            return {"status": "ok", "message": "Cookies updated successfully", "model": self.default_model}

    async def update_slot_cookie(
        self, slot: int, psid: str, psidts: str, label: str = ""
    ) -> dict:
        """Cập nhật cookie cho ô tài khoản chỉ định (slot 0..4)."""
        if self.state in ("starting", "stopping"):
            raise BridgeControlError("Đang có thao tác bật/tắt, không thể cập nhật cookie lúc này.")

        if slot < 0 or slot > 4:
            raise ValueError("Slot phải từ 0 đến 4.")

        new_key = account_key(psid)
        # Kiểm tra trùng với ô khác trong luồng này
        for idx, acc in enumerate(self.chain or []):
            if idx != slot and acc.get("key") == new_key:
                raise ValueError(f"Cookie trùng với ô tài khoản slot {idx} trong luồng này.")

        async with self.lock:
            # Nếu slot chưa có trong chuỗi nhưng slot-1 đã có -> cho phép tạo mới (nối đuôi)
            if slot == len(self.chain):
                default_lbl = f"backup{slot}" if slot > 0 else (INSTANCE_LABEL or "chinh")
                self.chain.append({
                    "label": label or default_lbl,
                    "psid": psid,
                    "psidts": psidts,
                    "key": new_key,
                })
            elif 0 <= slot < len(self.chain):
                if label:
                    self.chain[slot]["label"] = label
                self.chain[slot]["psid"] = psid
                self.chain[slot]["psidts"] = psidts
                self.chain[slot]["key"] = new_key
            else:
                raise ValueError(f"Chưa có slot {slot - 1}. Hãy nạp các slot theo thứ tự liên tục.")

            # Nếu ô này đang là tài khoản đang dùng:
            if slot == self.active_index:
                if self.state == "off":
                    self._active_psid = psid
                    self.client = GeminiClient(psid, psidts)
                    logger.info(f"[CONTROL] Đã lưu cookie mới cho slot {slot} (đang tắt); sẽ dùng khi bật bridge.")
                else:
                    # Bridge đang ON: thử khởi tạo client mới
                    try:
                        await self.client.close()
                    except Exception:
                        pass
                    self.client = GeminiClient(psid, psidts)
                    await self.client.init(
                        timeout=90, auto_close=False, close_delay=300, auto_refresh=True
                    )
                    self._active_psid = psid
                    ok = self._evaluate_auth()
                    self._refresh_available_models()
                    self._ensure_default_model()
                    self._create_new_chat_session()
                    clear_saved_session()
                    self.vision_disabled_until = 0.0
                    self._consecutive_auth_errors = 0
                    if not ok:
                        raise AuthError("Cookie mới không đăng nhập được (guest mode hoặc cookie hết hạn)")
                    logger.info(f"✅ Cookie slot {slot} đã được cập nhật thành công!")
            else:
                # Nếu ô này là ô dự phòng: xóa trạng thái exhausted (nếu có)
                if new_key in self.account_state:
                    self.account_state.pop(new_key, None)
                    save_state(INSTANCE_PATHS["accounts_state_file"], self.account_state)

            # Cập nhật chain file và kiểm tra trùng lặp giữa các luồng
            try:
                write_chain_file(
                    INSTANCE_PATHS["chain_file"],
                    INSTANCE_ID,
                    PORT,
                    [a["key"] for a in self.chain],
                    time.time(),
                )
                self.chain_overlap = find_chain_overlap(
                    BASE_DIR,
                    INSTANCE_PATHS["chain_file"],
                    [a["key"] for a in self.chain],
                )
                for x in self.chain_overlap:
                    logger.warning(
                        f"[ACCOUNTS] Tài khoản mới nạp ở slot {slot} trùng với luồng '{x.get('other_instance')}'."
                    )
            except Exception as exc:
                logger.warning(f"[ACCOUNTS] Lỗi ghi chain file sau khi nạp cookie: {exc}")

            auth_status = self.auth_ok if (slot == self.active_index and self.state == "on") else True
            return {
                "ok": True,
                "auth_ok": auth_status,
                "account_key": new_key,
            }

    def mark_current_exhausted(self, reason: str, hours: float) -> None:
        self.mark_exhausted_for(self.active_index, reason, hours)

    def mark_exhausted_for(self, index: int, reason: str, hours: float) -> None:
        if not self.chain or index >= len(self.chain):
            return
        key = self.chain[index]["key"]
        mark_exhausted(
            self.account_state,
            key,
            time.time() + hours * 3600,
            reason,
            time.time(),
        )
        save_state(INSTANCE_PATHS["accounts_state_file"], self.account_state)

    async def _switch_to_account(self, index: int) -> bool:
        """Đóng client cũ, dựng client mới bằng cookie của chain[index], init, kiểm tra đăng nhập,
        tạo chat MỚI. Trả True nếu thành công."""
        acc = self.chain[index]
        try:
            await self.client.close()
        except Exception:
            pass
        self.client = GeminiClient(acc["psid"], acc["psidts"])
        try:
            await self.client.init(
                timeout=90, auto_close=False, close_delay=300, auto_refresh=True
            )
        except Exception as exc:
            logger.error(f"[QUOTA] Init tài khoản '{acc['label']}' thất bại: {exc}")
            self.mark_exhausted_for(index, "auth", AUTH_COOLDOWN_MINUTES / 60.0)
            return False
        self._active_psid = acc["psid"]
        self.active_index = index
        self._refresh_available_models()
        self._ensure_default_model()
        if not self._evaluate_auth():
            self.mark_exhausted_for(index, "auth", AUTH_COOLDOWN_MINUTES / 60.0)
            return False
        self._create_new_chat_session()
        self.vision_disabled_until = 0.0
        self._consecutive_auth_errors = 0
        self._consecutive_transient_errors = 0
        self._using_backup = index > 0
        return True

    async def _rebuild_client(self, index: int) -> None:
        """Dựng lại client của tài khoản chain[index] để client và active_index luôn khớp nhau
        sau một lần failover thất bại (sau đó endpoint trả HTTP 503)."""
        if not self.chain or index >= len(self.chain):
            return
        acc = self.chain[index]
        try:
            await self.client.close()
        except Exception:
            pass
        self.client = GeminiClient(acc["psid"], acc["psidts"])
        self._active_psid = acc["psid"]
        self.active_index = index
        try:
            await self.client.init(
                timeout=60, auto_close=False, close_delay=300, auto_refresh=True
            )
        except Exception as exc:
            logger.error(f"[QUOTA] Không dựng lại được client '{acc['label']}': {exc}")
        self._evaluate_auth()

    async def _try_failover_to_backup(self) -> bool:
        """Chuyển sang tài khoản kế tiếp CÒN DÙNG ĐƯỢC. Thử lần lượt cho tới khi được hoặc hết."""
        if self.state == "off":
            return False
        start_index = self.active_index
        tried_any = False
        while True:
            idx = next_available_index(
                self.chain, self.account_state, self.active_index, time.time()
            )
            if idx is None:
                logger.critical(
                    "[QUOTA] Không còn tài khoản nào dùng được (tất cả đang nghỉ/lỗi)."
                )
                if tried_any:
                    await self._rebuild_client(start_index)
                return False
            tried_any = True
            if await self._switch_to_account(idx):
                logger.warning(
                    f"[QUOTA] Đã chuyển sang tài khoản '{self.chain[idx]['label']}'."
                )
                return True

    HANDOFF_PROMPT_TEMPLATE = (
        "You are writing a HANDOFF NOTE so another assistant can continue this coding task.\n"
        "Reply with EXACTLY these seven sections. Each header goes alone on its own line, "
        "in square brackets, exactly as written. Write nothing before the first header or after the last section.\n"
        "[GOAL]\n[DONE]\n[DOING]\n[NEXT]\n[DECISIONS]\n[FILES]\n[LAST_ERROR]\n"
        "Limits: GOAL max 3 lines; DONE max 8 bullets (oldest first, newest last); DOING max 3 lines; "
        "NEXT max 5 bullets; DECISIONS max 5 bullets (important decisions and why); "
        "FILES max 15 lines, file paths only, never file contents; LAST_ERROR max 2 lines, leave empty if none.\n"
        "Rules: be terse, no code blocks, no markdown headings, write in the language the user used.\n"
        "CONVERSATION (most recent last):\n{source}\n"
    )

    def _pick_flash_model(self):
        """Model đầu tiên trong self.available_models có chứa 'flash' (không phân biệt hoa thường), hoặc None."""
        for name in self.available_models or []:
            if "flash" in str(name).lower():
                return name
        return None

    async def _ask_flash_for_handoff(self) -> str:
        flash = self._pick_flash_model()
        if not flash:
            logger.warning("[QUOTA] Không thấy model Flash; dùng sổ sự thật.")
            return ""
        source = "\n".join(self.transcript)[-HANDOFF_SOURCE_MAX_CHARS:]
        prompt = self.HANDOFF_PROMPT_TEMPLATE.format(source=source)
        try:
            # Thư viện đã tự client.close() trước khi ném UsageLimitExceededError -> init lại
            await self.client.init(
                timeout=60, auto_close=False, close_delay=300, auto_refresh=True
            )
            kwargs = {}
            if flash and flash != "unspecified":
                kwargs["model"] = str(flash)
            chat = self.client.start_chat(**kwargs)
            out = await asyncio.wait_for(
                chat.send_message(prompt), timeout=HANDOFF_FLASH_TIMEOUT
            )
            return (getattr(out, "text", "") or "").strip()
        except Exception as exc:
            logger.warning(
                f"[QUOTA] Flash không viết được bản bàn giao ({exc}); dùng sổ sự thật."
            )
            return ""

    def _write_handoff_archive(self, bounded: str, flash_text: str) -> str:
        try:
            handoff_dir = Path(INSTANCE_PATHS["handoff_dir"])
            handoff_dir.mkdir(parents=True, exist_ok=True)
            now_str = datetime.now().strftime("%Y%m%d_%H%M%S")
            archive_file = handoff_dir / f"handoff_{now_str}.md"

            transcript_str = "\n".join(self.transcript[-60:])
            content = (
                f"# HANDOFF ARCHIVE ({now_str})\n\n"
                f"## Bounded Handoff Note\n{bounded}\n\n"
                f"## Flash Raw Output\n{flash_text or '(none)'}\n\n"
                f"## Fact Ledger\n```json\n"
                f"{json.dumps(self.ledger.data, ensure_ascii=False, indent=2)}\n```\n\n"
                f"## Recent Transcript\n```\n"
                f"{transcript_str}\n```\n"
            )
            archive_file.write_text(content, encoding="utf-8")

            latest_file = handoff_dir / "latest.md"
            latest_file.write_text(bounded, encoding="utf-8")

            # Keep max 20 handoff_*.md files
            files = sorted(handoff_dir.glob("handoff_*.md"))
            while len(files) > 20:
                oldest = files.pop(0)
                try:
                    oldest.unlink()
                except Exception:
                    pass

            return str(archive_file.resolve())
        except Exception as exc:
            logger.warning(f"[QUOTA] Không ghi được handoff archive: {exc}")
            return "(không ghi được archive)"

    async def _handle_quota_exhausted(self, exc) -> bool:
        """Pro của tài khoản hiện tại hết. Viết bản bàn giao, chuyển tài khoản kế. True nếu đã chuyển."""
        if self.state == "off":
            return False
        old = (
            self.chain[self.active_index]["label"]
            if (self.chain and 0 <= self.active_index < len(self.chain))
            else "chinh"
        )
        logger.warning(f"[QUOTA] Tài khoản '{old}' hết quota: {exc}")
        self.mark_current_exhausted("quota", QUOTA_COOLDOWN_HOURS)

        fallback = self.ledger.to_sections()
        try:
            flash_text = (
                await self._ask_flash_for_handoff() if HANDOFF_ENABLE else ""
            )
        except Exception as exc:
            logger.warning(f"[QUOTA] Flash handoff lỗi ({exc}); dùng sổ sự thật.")
            flash_text = ""
        parsed = parse_sections(flash_text) if flash_text else {}
        sections = {}
        for name in SECTION_ORDER:
            sections[name] = parsed.get(name) or fallback.get(name, [])
        bounded = bound_sections(sections, HANDOFF_MAX_CHARS)
        archive = self._write_handoff_archive(bounded, flash_text)
        self._pending_handoff = (bounded, archive)

        switched = await self._try_failover_to_backup()
        if switched:
            self._emit(
                event="account_switch",
                reason="quota",
                from_label=old,
                to_label=self.chain[self.active_index]["label"],
                handoff_chars=len(bounded),
                flash_used=bool(flash_text),
            )
        else:
            self._pending_handoff = None
        return switched

    async def reset_session(self) -> None:
        async with self.lock:
            self._create_new_chat_session()
            clear_saved_session()
            self.vision_disabled_until = 0.0
            logger.info(f"Đã làm mới phiên ChatSession (Model: {self.default_model}).")

    async def close(self) -> None:
        self._save_session()
        try:
            await self.client.close()
        except Exception:
            pass

    def get_status(self) -> Dict[str, Any]:
        """Trả về thông tin trạng thái chi tiết."""
        uptime = time.time() - self.start_time
        hours, remainder = divmod(int(uptime), 3600)
        minutes, seconds = divmod(remainder, 60)

        now = time.time()
        accounts_info = []
        for i, acc in enumerate(self.chain or []):
            entry = self.account_state.get(acc["key"])
            is_resting = False
            resting_until = None
            reason = None
            if entry:
                until = float(entry.get("until", 0))
                if now < until:
                    is_resting = True
                    try:
                        resting_until = datetime.fromtimestamp(until).astimezone().isoformat()
                    except Exception:
                        resting_until = str(until)
                    reason = entry.get("reason")

            st = "active" if i == self.active_index else ("resting" if is_resting else "ready")
            accounts_info.append({
                "label": acc["label"],
                "key": acc["key"],
                "status": st,
                "resting_until": resting_until,
                "reason": reason,
            })

        active_label = (
            self.chain[self.active_index]["label"]
            if (self.chain and 0 <= self.active_index < len(self.chain))
            else "unknown"
        )

        is_on = (self.state == "on")
        auth_ok = self.auth_ok if is_on else None
        account_status = self.account_status_name if is_on else None
        cookie_source = self.cookie_source if is_on else None

        enabled_since_iso = None
        if self.enabled_since:
            try:
                enabled_since_iso = datetime.fromtimestamp(self.enabled_since).astimezone().isoformat()
            except Exception:
                enabled_since_iso = str(self.enabled_since)

        status_str = "off" if self.state == "off" else ("ok" if self.auth_ok else "degraded")

        last_act = None
        if self.last_request_ts > 0:
            try:
                last_act = datetime.fromtimestamp(self.last_request_ts).astimezone().isoformat()
            except Exception:
                last_act = str(self.last_request_ts)

        return {
            "status": status_str,
            "state": self.state,
            "enabled": is_on,
            "enabled_since": enabled_since_iso,
            "auth_ok": auth_ok,
            "account_status": account_status,
            "cookie_source": cookie_source,
            "instance_id": INSTANCE_ID or "default",
            "instance_label": INSTANCE_LABEL,
            "port": PORT,
            "account_key": self.account_key,
            "accounts": accounts_info,
            "active_account": active_label,
            "chain_overlap": getattr(self, "chain_overlap", []),
            "model": self.default_model,
            "available_models": self.available_models,
            "model_details": self.model_details,
            "session_initialized": self.session_initialized,
            "turn_count": self.turn_count,
            "total_requests": self.total_requests,
            "total_errors": self.total_errors,
            "error_rate": f"{(self.total_errors / max(self.total_requests, 1)) * 100:.1f}%",
            "uptime": f"{hours}h {minutes}m {seconds}s",
            "uptime_seconds": int(uptime),
            "using_backup_cookie": self._using_backup,
            "vision_upload_available": self.vision_upload_available,
            "cache_entries": len(_response_cache),
            "cache_enabled": bool(ENABLE_RESPONSE_CACHE),
            "inflight": self._inflight,
            "chat_key": self.current_chat_key(),
            "last_activity_ts": last_act,
        }

    async def send_prepared_turn(
        self, prompt_to_send: str, image_files: List[str]
    ) -> str:
        """Gửi 1 lượt hội thoại vào Gemini Web với đầy đủ cơ chế bảo vệ."""
        self.require_on()
        self._inflight += 1
        self._idle_event.clear()
        try:
            return await self._send_prepared_turn_inner(prompt_to_send, image_files)
        finally:
            self._inflight -= 1
            if self._inflight <= 0:
                self._inflight = 0
                self._idle_event.set()

    async def _send_prepared_turn_inner(
        self, prompt_to_send: str, image_files: List[str]
    ) -> str:
        self.total_requests += 1

        # Kiểm tra cache (chỉ khi bật cache và prompt không có ảnh)
        if ENABLE_RESPONSE_CACHE and not image_files:
            cached = get_cached_response(prompt_to_send, self.default_model)
            if cached:
                return cached

        # Lock với timeout — trả lỗi thay vì chờ vô hạn (M2)
        try:
            await asyncio.wait_for(self.lock.acquire(), timeout=LOCK_ACQUIRE_TIMEOUT)
        except asyncio.TimeoutError:
            cleanup_temp_images(image_files)
            self.total_errors += 1
            raise BridgeBusyError("Bridge đang bận, thử lại sau vài giây.")

        try:
            # Giãn cách an toàn
            elapsed = time.time() - self.last_request_ts
            if elapsed < MIN_REQUEST_INTERVAL:
                await asyncio.sleep(MIN_REQUEST_INTERVAL - elapsed)

            if self.chat_session is None:
                self._create_new_chat_session()

            base_prompt = prompt_to_send

            # ─────────────────────────────────────────────────────
            # TẦNG 1: Gửi ảnh trực tiếp lên Gemini (nếu có ảnh)
            # ─────────────────────────────────────────────────────
            if image_files and self.vision_upload_available:
                try:
                    logger.info(
                        f"[Vision Layer 1] Upload {len(image_files)} ảnh lên Gemini..."
                    )
                    prompt_with_header = self._ensure_header_in_prompt(
                        self._apply_pending_handoff(base_prompt)
                    )
                    coro = self.chat_session.send_message(
                        prompt_with_header, files=image_files
                    )
                    response = await asyncio.wait_for(
                        coro, timeout=GEMINI_HARD_TIMEOUT
                    )
                    self.last_request_ts = time.time()
                    reply = (response.text or "").strip()
                    del response
                    self.session_initialized = True
                    self.tool_header_sent = True
                    self.turn_count += 1
                    self._consecutive_auth_errors = 0
                    self._consecutive_transient_errors = 0
                    self._pending_handoff = None
                    self._save_session()
                    return reply
                except Exception as img_exc:
                    if self._is_auth_error(img_exc) or not self._evaluate_auth():
                        raise BridgeAuthError("Gemini chưa đăng nhập (guest/cookie hết hạn).")
                    logger.warning(
                        f"Upload ảnh thất bại ({img_exc}). --> Chuyển sang Local OCR!"
                    )
                    self.vision_disabled_until = time.time() + 300

            # ─────────────────────────────────────────────────────
            # TẦNG 2: Local OCR fallback (cross-platform)
            # ─────────────────────────────────────────────────────
            if image_files:
                ocr_blocks = []
                for idx, img_p in enumerate(image_files, 1):
                    logger.info(
                        f"[Vision Layer 2] Quét chữ từ ảnh #{idx} bằng Local OCR..."
                    )
                    ocr_text = await asyncio.to_thread(
                        extract_text_from_image_local, img_p
                    )
                    ocr_blocks.append(
                        f"=== [EXTRACTED TEXT FROM IMAGE #{idx}] ===\n"
                        f"{ocr_text}\n"
                        f"========================================"
                    )
                base_prompt = (
                    "\n\n".join(ocr_blocks)
                    + "\n\n(Above is text extracted from the user's screenshot. "
                    "Analyze and respond in the SAME LANGUAGE as the user.)\n\n"
                    + base_prompt
                )

            # ─────────────────────────────────────────────────────
            # TẦNG 3: Gửi qua cổng Text (luôn sống với 1PSID)
            # ─────────────────────────────────────────────────────
            attempt = 0
            quota_switches = 0
            while attempt < 2:
                prompt_to_send = self._ensure_header_in_prompt(
                    self._apply_pending_handoff(base_prompt)
                )
                try:
                    if BRIDGE_DEBUG_FAKE_QUOTA and not self._fake_quota_done:
                        self._fake_quota_done = True
                        raise UsageLimitExceededError("DEBUG: giả lập hết quota (BRIDGE_DEBUG_FAKE_QUOTA)")

                    logger.info(
                        f"Gửi vào ChatSession (Turn={self.turn_count + 1} | "
                        f"Chars={len(prompt_to_send)})"
                    )
                    coro = self.chat_session.send_message(prompt_to_send)
                    response = await asyncio.wait_for(
                        coro, timeout=GEMINI_HARD_TIMEOUT
                    )
                    self.last_request_ts = time.time()
                    reply = (response.text or "").strip()
                    del response

                    self.session_initialized = True
                    self.tool_header_sent = True
                    self.turn_count += 1
                    self._consecutive_auth_errors = 0
                    self._consecutive_transient_errors = 0
                    self._pending_handoff = None
                    self._save_session()

                    # Cache kết quả (chỉ text response, không cache tool calls)
                    if ENABLE_RESPONSE_CACHE and not image_files and "<tool_call>" not in reply:
                        set_cached_response(prompt_to_send, reply, self.default_model)

                    # Dọn bộ đệm ngầm mỗi 15 lượt
                    if (
                        self.turn_count % 15 == 0
                        and hasattr(self.chat_session, "metadata")
                    ):
                        saved_meta = list(self.chat_session.metadata)
                        self._create_new_chat_session(preserve_metadata=saved_meta)

                    return reply

                except asyncio.TimeoutError:
                    logger.error(f"Quá thời gian {GEMINI_HARD_TIMEOUT}s!")
                    self.total_errors += 1
                    raise BridgeTimeoutError(
                        f"Gemini không phản hồi sau {GEMINI_HARD_TIMEOUT}s."
                    )

                except UsageLimitExceededError as exc:
                    logger.warning("[QUOTA] đang chuyển tài khoản, có thể mất 1-3 phút")
                    if quota_switches < len(self.chain) and await self._handle_quota_exhausted(exc):
                        quota_switches += 1
                        continue
                    raise BridgeQuotaError("Tất cả tài khoản đều hết quota. Hãy thử lại sau hoặc nạp thêm tài khoản.")

                except Exception as exc:
                    self.total_errors += 1
                    if self._is_auth_error(exc):
                        self._consecutive_auth_errors += 1
                    else:
                        self._consecutive_transient_errors += 1
                    logger.warning(
                        f"Lỗi ChatSession (Lần {attempt + 1}/2): {exc}"
                    )

                    # Cảnh báo cookie hết hạn
                    if self._consecutive_auth_errors >= 2:
                        logger.critical(
                            "⚠️ COOKIE CÓ THỂ HẾT HẠN! "
                            "Cập nhật cookie trong .env hoặc gọi POST /v1/update-cookies"
                        )
                        if await self._try_failover_to_backup():
                            continue

                    if attempt == 0:
                        saved_meta = self._chat_metadata_snapshot()
                        try:
                            await self.client.close()
                        except Exception:
                            pass
                        try:
                            await self.client.init(
                                timeout=60,
                                auto_close=False,
                                close_delay=300,
                                auto_refresh=True,
                            )
                        except Exception as init_exc:
                            logger.error(f"Init lại client thất bại: {init_exc}")
                            raise BridgeUpstreamError(f"Lỗi kết nối Gemini Web: {init_exc}")
                        if not self._evaluate_auth():
                            if await self._try_failover_to_backup():
                                attempt += 1
                                continue
                            raise BridgeAuthError("Gemini chưa đăng nhập (guest/cookie hết hạn).")
                        self._create_new_chat_session(preserve_metadata=saved_meta)
                        attempt += 1
                    else:
                        raise BridgeUpstreamError(f"Lỗi kết nối Gemini Web: {exc}")

            raise BridgeUpstreamError("Không nhận được phản hồi.")
        finally:
            self.lock.release()
            cleanup_temp_images(image_files)

    async def send_prepared_turn_stream(
        self, prompt_to_send: str, image_files: List[str]
    ):
        """Streaming variant of send_prepared_turn.

        Yields ``("delta", text)`` for incremental text, ``("text", msg)`` for a
        standalone message (busy/error), and always finishes with ``("done",
        full_text)``.
        """
        self.require_on()
        self._inflight += 1
        self._idle_event.clear()
        try:
            async for item in self._send_prepared_turn_stream_inner(prompt_to_send, image_files):
                yield item
        finally:
            self._inflight -= 1
            if self._inflight <= 0:
                self._inflight = 0
                self._idle_event.set()

    async def _send_prepared_turn_stream_inner(
        self, prompt_to_send: str, image_files: List[str]
    ):
        self.total_requests += 1

        try:
            await asyncio.wait_for(self.lock.acquire(), timeout=LOCK_ACQUIRE_TIMEOUT)
        except asyncio.TimeoutError:
            cleanup_temp_images(image_files)
            self.total_errors += 1
            yield "error", {
                "message": "Bridge đang bận, thử lại sau vài giây.",
                "type": BridgeBusyError.err_type,
                "code": BridgeBusyError.code,
            }
            return

        full_text = ""
        try:
            elapsed = time.time() - self.last_request_ts
            if elapsed < MIN_REQUEST_INTERVAL:
                await asyncio.sleep(MIN_REQUEST_INTERVAL - elapsed)

            if self.chat_session is None:
                self._create_new_chat_session()

            base_prompt = prompt_to_send

            # Tầng 1: stream kèm ảnh trực tiếp (nếu có)
            if image_files and self.vision_upload_available:
                try:
                    logger.info(
                        f"[Vision Layer 1] Stream {len(image_files)} ảnh lên Gemini..."
                    )
                    prompt_with_header = self._ensure_header_in_prompt(
                        self._apply_pending_handoff(base_prompt)
                    )
                    async with _asyncio_timeout(GEMINI_HARD_TIMEOUT):
                        async for output in self.chat_session.send_message_stream(
                            prompt_with_header, files=image_files
                        ):
                            delta = output.text_delta or ""
                            if delta:
                                full_text += delta
                                yield "delta", delta
                    self.last_request_ts = time.time()
                    self.session_initialized = True
                    self.tool_header_sent = True
                    self.turn_count += 1
                    self._consecutive_auth_errors = 0
                    self._consecutive_transient_errors = 0
                    self._pending_handoff = None
                    self._save_session()
                    yield "done", full_text.strip()
                    return
                except asyncio.TimeoutError:
                    logger.error(f"[Vision] Quá thời gian {GEMINI_HARD_TIMEOUT}s!")
                    self.total_errors += 1
                    self.vision_disabled_until = time.time() + 300
                    full_text = ""
                except Exception as img_exc:
                    if self._is_auth_error(img_exc) or not self._evaluate_auth():
                        yield "error", {
                            "message": "Gemini chưa đăng nhập (guest/cookie hết hạn).",
                            "type": BridgeAuthError.err_type,
                            "code": BridgeAuthError.code,
                        }
                        return
                    logger.warning(
                        f"Stream ảnh thất bại ({img_exc}). --> Chuyển sang Local OCR!"
                    )
                    self.vision_disabled_until = time.time() + 300
                    full_text = ""

            # Tầng 2: OCR fallback
            if image_files:
                ocr_blocks = []
                for idx, img_p in enumerate(image_files, 1):
                    logger.info(
                        f"[Vision Layer 2] Quét chữ từ ảnh #{idx} bằng Local OCR..."
                    )
                    ocr_text = await asyncio.to_thread(
                        extract_text_from_image_local, img_p
                    )
                    ocr_blocks.append(
                        f"=== [EXTRACTED TEXT FROM IMAGE #{idx}] ===\n"
                        f"{ocr_text}\n"
                        f"========================================"
                    )
                base_prompt = (
                    "\n\n".join(ocr_blocks)
                    + "\n\n(Above is text extracted from the user's screenshot. "
                    "Analyze and respond in the SAME LANGUAGE as the user.)\n\n"
                    + base_prompt
                )

            # Tầng 3: stream text
            attempt = 0
            quota_switches = 0
            yielded_any = False
            while attempt < 2:
                prompt_to_send = self._ensure_header_in_prompt(
                    self._apply_pending_handoff(base_prompt)
                )
                try:
                    if BRIDGE_DEBUG_FAKE_QUOTA and not self._fake_quota_done:
                        self._fake_quota_done = True
                        raise UsageLimitExceededError("DEBUG: giả lập hết quota (BRIDGE_DEBUG_FAKE_QUOTA)")

                    logger.info(
                        f"Stream vào ChatSession (Turn={self.turn_count + 1} | "
                        f"Chars={len(prompt_to_send)})"
                    )
                    async with _asyncio_timeout(GEMINI_HARD_TIMEOUT):
                        async for output in self.chat_session.send_message_stream(
                            prompt_to_send
                        ):
                            delta = output.text_delta or ""
                            if delta:
                                yielded_any = True
                                full_text += delta
                                yield "delta", delta
                    self.last_request_ts = time.time()
                    reply = full_text.strip()
                    self.session_initialized = True
                    self.tool_header_sent = True
                    self.turn_count += 1
                    self._consecutive_auth_errors = 0
                    self._consecutive_transient_errors = 0
                    self._pending_handoff = None
                    self._save_session()

                    if ENABLE_RESPONSE_CACHE and not image_files and "<tool_call>" not in reply:
                        set_cached_response(prompt_to_send, reply, self.default_model)

                    if self.turn_count % 15 == 0 and hasattr(self.chat_session, "metadata"):
                        saved_meta = list(self.chat_session.metadata)
                        self._create_new_chat_session(preserve_metadata=saved_meta)

                    yield "done", reply
                    return

                except asyncio.TimeoutError:
                    logger.error(f"Quá thời gian {GEMINI_HARD_TIMEOUT}s!")
                    self.total_errors += 1
                    yield "error", {
                        "message": f"Gemini không phản hồi sau {GEMINI_HARD_TIMEOUT}s.",
                        "type": BridgeTimeoutError.err_type,
                        "code": BridgeTimeoutError.code,
                    }
                    return

                except UsageLimitExceededError as exc:
                    if yielded_any:
                        yield "error", {
                            "message": f"Hết quota khi đang stream: {exc}",
                            "type": BridgeQuotaError.err_type,
                            "code": BridgeQuotaError.code,
                        }
                        return
                    if quota_switches < len(self.chain):
                        task = asyncio.create_task(self._handle_quota_exhausted(exc))
                        while not task.done():
                            yield "delta", ""
                            await asyncio.sleep(2.5)
                        switched = task.result()
                        if switched:
                            quota_switches += 1
                            continue
                    yield "error", {
                        "message": "Tất cả tài khoản đều hết quota. Hãy thử lại sau hoặc nạp thêm tài khoản.",
                        "type": BridgeQuotaError.err_type,
                        "code": BridgeQuotaError.code,
                    }
                    return

                except Exception as exc:
                    self.total_errors += 1
                    if self._is_auth_error(exc):
                        self._consecutive_auth_errors += 1
                    else:
                        self._consecutive_transient_errors += 1
                    logger.warning(
                        f"Lỗi ChatSession stream (Lần {attempt + 1}/2): {exc}"
                    )
                    if self._consecutive_auth_errors >= 2:
                        logger.critical(
                            "⚠️ COOKIE CÓ THỂ HẾT HẠN! "
                            "Cập nhật cookie trong .env hoặc gọi POST /v1/update-cookies"
                        )
                        if await self._try_failover_to_backup():
                            continue
                    if attempt == 0:
                        saved_meta = self._chat_metadata_snapshot()
                        try:
                            await self.client.close()
                        except Exception:
                            pass
                        try:
                            await self.client.init(
                                timeout=60, auto_close=False, close_delay=300, auto_refresh=True
                            )
                        except Exception as init_exc:
                            logger.error(f"Init lại client thất bại: {init_exc}")
                            yield "error", {
                                "message": f"Lỗi kết nối Gemini Web: {init_exc}",
                                "type": BridgeUpstreamError.err_type,
                                "code": BridgeUpstreamError.code,
                            }
                            return
                        if not self._evaluate_auth():
                            if await self._try_failover_to_backup():
                                attempt += 1
                                continue
                            yield "error", {
                                "message": "Gemini chưa đăng nhập (guest/cookie hết hạn).",
                                "type": BridgeAuthError.err_type,
                                "code": BridgeAuthError.code,
                            }
                            return
                        self._create_new_chat_session(preserve_metadata=saved_meta)
                        full_text = ""
                        attempt += 1
                    else:
                        yield "error", {
                            "message": f"Lỗi kết nối Gemini Web: {exc}",
                            "type": BridgeUpstreamError.err_type,
                            "code": BridgeUpstreamError.code,
                        }
                        return

            yield "error", {
                "message": "Không nhận được phản hồi từ Gemini.",
                "type": BridgeUpstreamError.err_type,
                "code": BridgeUpstreamError.code,
            }
        finally:
            self.lock.release()
            cleanup_temp_images(image_files)


# Singleton instance
session_manager = SingleChatManager()
