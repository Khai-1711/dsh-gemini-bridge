"""Lõi điều phối hội thoại Gemini Web (transport, retry, model switch, streaming)."""
import asyncio
import contextlib
import sys
import time
from typing import Any, Callable, Dict, List, Optional

from gemini_webapi.exceptions import UsageLimitExceededError

from app.account_pool import AccountPoolMixin, first_available_index, load_state
from app.accounts import account_key, find_chain_overlap, write_chain_file
from app.config import (
    ACCOUNT_CHAIN,
    ALLOW_GUEST_MODE,
    BASE_DIR,
    BRIDGE_DEBUG_FAKE_QUOTA,
    INSTANCE_ID,
    LOCK_ACQUIRE_TIMEOUT,
    PORT,
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
from app.handoff import FactLedger
from app.handoff_runner import HandoffRunnerMixin
from app.session_pool import (
    SessionPoolMixin,
    _tool_header_fingerprint,
    load_saved_session,
)
from app.status_view import build_manager_status
from app.vision import cleanup_temp_images


def _gs():
    return sys.modules["app.gemini_session"]


def _request_deadline(timeout: float) -> float:
    return asyncio.get_running_loop().time() + timeout


def _remaining_timeout(deadline: float, maximum: Optional[float] = None) -> float:
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        raise asyncio.TimeoutError
    return min(remaining, maximum) if maximum is not None else remaining


async def _await_with_deadline(awaitable, deadline: Optional[float]):
    if deadline is None:
        return await awaitable
    return await asyncio.wait_for(awaitable, timeout=_remaining_timeout(deadline))


async def _iterate_stream_until(
    stream,
    deadline: float,
    first_chunk_deadline: Optional[float] = None,
    chunk_idle_timeout: Optional[float] = None,
    overall_deadline: Optional[float] = None,
):
    """Iterate an async response stream with initial, per-chunk idle, and overall deadlines."""
    iterator = stream.__aiter__()
    got_activity = False
    stream_cap = overall_deadline if overall_deadline is not None else deadline
    while True:
        if not got_activity:
            active_deadline = (
                min(deadline, first_chunk_deadline)
                if first_chunk_deadline is not None
                else deadline
            )
        else:
            active_deadline = stream_cap
            if chunk_idle_timeout is not None:
                active_deadline = min(
                    stream_cap, asyncio.get_running_loop().time() + chunk_idle_timeout
                )
        try:
            item = await asyncio.wait_for(
                iterator.__anext__(), timeout=_remaining_timeout(active_deadline)
            )
        except StopAsyncIteration:
            return
        if getattr(item, "text_delta", None) or getattr(item, "thoughts_delta", None):
            got_activity = True
        yield item


class SingleChatManager(SessionPoolMixin, AccountPoolMixin, HandoffRunnerMixin):
    """Quản lý phiên hội thoại với Gemini Web: lock tuần tự theo tài khoản, failover, streaming."""

    HEADER_MARKER = "=== WORKSPACE TOOLS"

    def __init__(self) -> None:
        gs = _gs()
        paths = gs.INSTANCE_PATHS
        self.chain = ACCOUNT_CHAIN
        self.account_state = load_state(paths["accounts_state_file"])
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
        self.client = gs.GeminiClient(initial_acc["psid"], initial_acc["psidts"])
        self.lock = asyncio.Lock()
        self._lock_owner_task: Optional[asyncio.Task] = None
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
        self.chat_tool_header_hash = ""
        self.chat_session = None
        self.session_initialized = False
        self.last_request_ts = 0.0
        self.turn_count = 0
        self._session_pool = {}
        self._active_session_key: str = "default"
        self._active_is_subagent: bool = False
        self.total_requests = 0
        self.total_errors = 0
        self.start_time = time.time()
        self._consecutive_auth_errors = 0
        self._consecutive_transient_errors = 0
        self._using_backup = self.active_index > 0
        self.allow_guest: bool = ALLOW_GUEST_MODE
        self.is_guest: bool = False
        self.auth_ok: bool = False
        self.account_status_name: str = "UNKNOWN"
        self.cookie_source: str = ""
        self._active_psid: str = initial_acc["psid"]
        self._last_recover_ts: float = 0.0
        self.last_tool_header: str = ""
        self.last_turn_model: str = ""
        self.last_turn_fallback_from: Optional[str] = None
        self.last_turn_prompt_chars: int = 0

        try:
            write_chain_file(
                paths["chain_file"],
                INSTANCE_ID,
                PORT,
                [a["key"] for a in self.chain],
                time.time(),
            )
            self.chain_overlap = find_chain_overlap(
                BASE_DIR, paths["chain_file"], [a["key"] for a in self.chain]
            )
            for x in self.chain_overlap:
                logger.critical(
                    f"[ACCOUNTS] Một tài khoản trong chuỗi của con '{INSTANCE_ID or 'default'}' "
                    f"cũng nằm trong chuỗi của con '{x.get('other_instance')}'. Hai con sẽ dùng chung một tài khoản."
                )
        except Exception as exc:
            self.chain_overlap = []
            logger.warning(f"[ACCOUNTS] Lỗi ghi chain file: {exc}")

        self._lead_ledger = FactLedger(
            paths["handoff_dir"], filename="ledger.json", persist=True
        )
        self.ledger = self._lead_ledger
        self.transcript: list = []
        self._pending_handoff = None
        self._fake_quota_done = False
        self.on_event = None

    @property
    def vision_upload_available(self) -> bool:
        return not self.is_guest

    async def acquire_turn_lock(self, timeout: float = LOCK_ACQUIRE_TIMEOUT) -> bool:
        """Lấy khóa tuần tự theo lượt; cho phép cùng coroutine task giữ trọn 1 lượt."""
        cur_task = asyncio.current_task()
        if (
            self.lock.locked()
            and cur_task is not None
            and getattr(self, "_lock_owner_task", None) is cur_task
        ):
            return False
        await asyncio.wait_for(self.lock.acquire(), timeout=timeout)
        self._lock_owner_task = cur_task
        return True

    def release_turn_lock(self, acquired_here: bool = True) -> None:
        if acquired_here and self.lock.locked():
            self._lock_owner_task = None
            self.lock.release()

    def _emit(self, **fields) -> None:
        if self.on_event:
            try:
                self.on_event(**fields)
            except Exception:
                pass

    def remember_transcript(self, messages) -> None:
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
        header = header or ""
        header_hash = _tool_header_fingerprint(header)
        if header_hash != self.chat_tool_header_hash:
            self.tool_header_sent = False
        elif header_hash:
            self.tool_header_sent = True
        self.last_tool_header = header

    def _mark_tool_header_sent(self, include_tool_header: bool) -> None:
        if include_tool_header:
            self.tool_header_sent = True
            self.chat_tool_header_hash = _tool_header_fingerprint(
                self.last_tool_header
            )

    def _ensure_header_in_prompt(
        self, prompt: str, include_tool_header: bool = True
    ) -> str:
        if (
            include_tool_header
            and self.needs_tool_header
            and self.last_tool_header
            and self.HEADER_MARKER not in prompt
        ):
            return prompt + "\n\n=== WORKSPACE TOOLS ===\n" + self.last_tool_header
        return prompt

    @property
    def needs_tool_header(self) -> bool:
        return (not self.session_initialized) or (not self.tool_header_sent)

    def require_on(self) -> None:
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
        drain_timeout = _gs().DISABLE_DRAIN_TIMEOUT
        async with self._control_lock:
            if self.state == "off":
                return
            self.state = "stopping"
            logger.info("[CONTROL] Đang TẮT bridge, chờ request đang chạy...")
            try:
                await asyncio.wait_for(self._idle_event.wait(), timeout=drain_timeout)
            except asyncio.TimeoutError:
                self.state = "on"
                logger.warning("[CONTROL] Còn request đang chạy; hủy việc tắt.")
                raise BridgeControlError(
                    f"Còn request đang chạy quá {int(drain_timeout)}s nên chưa tắt được. "
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
        self._refresh_available_models()
        self._ensure_default_model()
        logger.info(f"Model khả dụng ({len(self.available_models)}): {self.available_models}")

        self._consecutive_auth_errors = 0
        if not self._evaluate_auth() and len(self.chain) > 1:
            await self._try_failover_to_backup()

        if not self.auth_ok:
            self._create_new_chat_session()
            msg = "ở chế độ KHÁCH (Guest Mode)" if (self.allow_guest and self.is_guest) else "chưa xác thực"
            logger.info(f"SingleChatManager sẵn sàng ({msg})! Model: [{self.default_model}]")
            return

        saved = load_saved_session()
        if saved is not None and saved.get("account_key") != self.account_key:
            logger.warning("Bỏ qua .bridge_session.json: thuộc tài khoản khác.")
            saved = None
        if saved is not None and self._resume_saved_session(saved):
            logger.info(
                f"SingleChatManager sẵn sàng! (đã khôi phục phiên chat cũ) Model: [{self.default_model}]"
            )
        else:
            self._create_new_chat_session()
            logger.info(f"SingleChatManager sẵn sàng! Model: [{self.default_model}]")

    def _refresh_available_models(self) -> List[str]:
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

        try:
            for model in self.client.list_models() or []:
                raw_n = getattr(model, "model_name", None) or getattr(model, "name", None) or model
                _add(raw_n, getattr(model, "display_name", None), getattr(model, "description", None))
        except Exception as e:
            logger.warning(f"Không đọc được model registry: {e}")

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
                            getattr(item, "model_name", None) or getattr(item, "name", None) or item,
                            getattr(item, "display_name", None),
                            getattr(item, "description", None),
                        )
                if details:
                    break

        if not details:
            _add("unspecified")

        has_gen4 = any(
            any(k in d["name"].lower() for k in ("4-", "neon", "hydrogen", "carbon"))
            for d in details
        )
        if has_gen4:
            details = [d for d in details if d["name"] not in ("gemini-flash-lite", "gemini-pro")]

        self.model_details = details
        self.available_models = [d["name"] for d in details]
        return self.available_models

    def _ensure_default_model(self) -> None:
        selectable = [m for m in self.available_models if m != "unspecified"]
        current = str(self.default_model or "").strip()
        if current and current not in ("unspecified", "gemini-flash-lite", "gemini-pro") and current in selectable:
            return
        for preferred in ("gemini-4-neon", "gemini-4-hydrogen", "gemini-carbon"):
            if preferred in selectable:
                self.default_model = preferred
                return
        if current and current != "unspecified" and current in selectable:
            return
        self.default_model = selectable[0] if selectable else "unspecified"

    def _select_fallback_model(self) -> str:
        current = str(self.default_model or "").strip()
        available = self.available_models or []
        for preferred in ("gemini-4-hydrogen", "gemini-4-neon"):
            if preferred != current and preferred in available:
                return preferred
        if current != "unspecified":
            return "unspecified"
        for candidate in available:
            if candidate and candidate != current:
                return candidate
        return "unspecified"

    async def switch_model(self, new_model: str, keep_chat: bool = True) -> str:
        async with self.lock:
            old_model = self.default_model
            target = str(new_model).strip()
            if target and target != "unspecified":
                try:
                    resolved = self.client.resolve_model(target)
                    if target not in self.available_models:
                        target = str(getattr(resolved, "model_name", None) or target)
                except Exception:
                    if self.available_models and target not in self.available_models:
                        raise ValueError(
                            f"Model '{target}' không có trong tài khoản. "
                            f"Model khả dụng: {', '.join(self.available_models)}"
                        )
            if target == str(self.default_model or "").strip() and keep_chat and self.chat_session is not None:
                logger.info(f"[MODEL] [{target}] đã hoạt động; giữ nguyên ChatSession.")
                return self.default_model
            self.default_model = target
            saved_meta = (
                list(self.chat_session.metadata)
                if (keep_chat and self.chat_session and hasattr(self.chat_session, "metadata"))
                else None
            )
            self._create_new_chat_session(preserve_metadata=saved_meta)
            self.tool_header_sent = False
            self.chat_tool_header_hash = ""
            self._save_session()
            self._sync_active_session_slot()
            logger.info(f"==> CHUYỂN MODEL: [{old_model}] --> [{self.default_model}]")
            return self.default_model

    async def close(self) -> None:
        self._save_session()
        try:
            await self.client.close()
        except Exception:
            pass

    def get_status(self) -> Dict[str, Any]:
        return build_manager_status(self)

    @staticmethod
    def _bridge_error_from_payload(payload: Any) -> BridgeError:
        if not isinstance(payload, dict):
            return BridgeUpstreamError(str(payload or "Streaming failed"))
        cls = {
            BridgeAuthError.code: BridgeAuthError,
            BridgeBusyError.code: BridgeBusyError,
            BridgeTimeoutError.code: BridgeTimeoutError,
            BridgeQuotaError.code: BridgeQuotaError,
            BridgeDisabledError.code: BridgeDisabledError,
            BridgeControlError.code: BridgeControlError,
            BridgeUpstreamError.code: BridgeUpstreamError,
        }.get(str(payload.get("code") or ""), BridgeUpstreamError)
        return cls(str(payload.get("message") or "Không nhận được phản hồi từ Gemini."), stage=payload.get("stage"))

    def _open_chat_stream(self, prompt_to_send: str, image_files: List[str]):
        stream_fn = getattr(self.chat_session, "send_message_stream", None)
        if callable(stream_fn):
            return stream_fn(prompt_to_send, files=image_files) if image_files else stream_fn(prompt_to_send)
        send_fn = getattr(self.chat_session, "send_message", None)
        if not callable(send_fn):
            raise RuntimeError("ChatSession does not expose send_message_stream or send_message.")

        async def _single_shot_stream():
            resp = await (send_fn(prompt_to_send, files=image_files) if image_files else send_fn(prompt_to_send))
            yield type("Delta", (), {"text_delta": getattr(resp, "text", None) or ""})()

        return _single_shot_stream()

    async def _recover_client_after_error(
        self,
        stage: str,
        image_deadline: Optional[float],
        failover_attempts: int,
        max_failover_attempts: int,
        raise_init_error: bool,
    ) -> tuple[bool, Optional[dict], int]:
        """Khởi tạo lại GeminiClient và thử failover nếu mất phiên sau lỗi trước first-byte."""
        try:
            await _await_with_deadline(self.client.close(), image_deadline)
        except Exception:
            pass
        try:
            await _await_with_deadline(
                self.client.init(timeout=60, auto_close=False, close_delay=300, auto_refresh=True),
                image_deadline,
            )
        except Exception as init_exc:
            if image_deadline is not None and image_deadline <= asyncio.get_running_loop().time():
                raise asyncio.TimeoutError from init_exc
            if raise_init_error:
                logger.error(f"Init lại client thất bại: {init_exc}")
                return False, {
                    "message": f"Lỗi kết nối Gemini Web: {init_exc}",
                    "type": BridgeUpstreamError.err_type,
                    "code": BridgeUpstreamError.code,
                    "stage": stage,
                }, failover_attempts
            raise

        if not self._evaluate_auth():
            if (
                failover_attempts < max_failover_attempts
                and len(self.chain) > 1
                and await _await_with_deadline(self._try_failover_to_backup(), image_deadline)
            ):
                return True, None, failover_attempts + 1
            if not (self.allow_guest and self.is_guest):
                return False, {
                    "message": "Gemini chưa đăng nhập (guest/cookie hết hạn).",
                    "type": BridgeAuthError.err_type,
                    "code": BridgeAuthError.code,
                    "stage": stage,
                }, failover_attempts
        return True, None, failover_attempts

    async def send_prepared_turn(
        self,
        prompt_to_send: str = "",
        image_files: Optional[List[str]] = None,
        include_tool_header: bool = True,
        turn_preparer: Optional[Callable[["SingleChatManager"], Any]] = None,
    ) -> str:
        """Gửi 1 lượt hội thoại vào Gemini Web (bọc trực tiếp trên luồng stream thống nhất)."""
        self.require_on()
        self._inflight += 1
        self._idle_event.clear()
        try:
            return await self._send_prepared_turn_inner(
                prompt_to_send, list(image_files or []), include_tool_header, turn_preparer=turn_preparer
            )
        finally:
            self._inflight -= 1
            if self._inflight <= 0:
                self._inflight = 0
                self._idle_event.set()

    async def _send_prepared_turn_inner(
        self,
        prompt_to_send: str,
        image_files: List[str],
        include_tool_header: bool = True,
        turn_preparer: Optional[Callable[["SingleChatManager"], Any]] = None,
    ) -> str:
        reply_text = ""
        async with contextlib.aclosing(
            self._send_prepared_turn_stream_inner(
                prompt_to_send, image_files, include_tool_header, turn_preparer=turn_preparer
            )
        ) as stream:
            async for kind, payload in stream:
                if kind == "error":
                    raise self._bridge_error_from_payload(payload)
                if kind == "done":
                    reply_text = payload
        return reply_text

    async def send_prepared_turn_stream(
        self,
        prompt_to_send: str = "",
        image_files: Optional[List[str]] = None,
        include_tool_header: bool = True,
        turn_preparer: Optional[Callable[["SingleChatManager"], Any]] = None,
    ):
        self.require_on()
        self._inflight += 1
        self._idle_event.clear()
        try:
            async with contextlib.aclosing(
                self._send_prepared_turn_stream_inner(
                    prompt_to_send, list(image_files or []), include_tool_header, turn_preparer=turn_preparer
                )
            ) as stream:
                async for item in stream:
                    yield item
        finally:
            self._inflight -= 1
            if self._inflight <= 0:
                self._inflight = 0
                self._idle_event.set()

    async def _send_prepared_turn_stream_inner(
        self,
        prompt_to_send: str,
        image_files: List[str],
        include_tool_header: bool = True,
        turn_preparer: Optional[Callable[["SingleChatManager"], Any]] = None,
    ):
        gs = _gs()
        img_timeout = gs.IMAGE_REQUEST_TIMEOUT
        hard_timeout = gs.GEMINI_HARD_TIMEOUT
        min_interval = gs.MIN_REQUEST_INTERVAL
        first_byte_timeout = gs.MODEL_FIRST_BYTE_FALLBACK_TIMEOUT
        chunk_idle_timeout = gs.GEMINI_CHUNK_IDLE_TIMEOUT
        stream_overall_timeout = gs.GEMINI_STREAM_OVERALL_TIMEOUT

        self.total_requests += 1
        stage = "lock"
        image_files = list(image_files or [])
        image_deadline = _request_deadline(img_timeout) if image_files else None

        try:
            lock_timeout = (
                _remaining_timeout(image_deadline, LOCK_ACQUIRE_TIMEOUT)
                if image_deadline is not None
                else LOCK_ACQUIRE_TIMEOUT
            )
            acquired_here = await self.acquire_turn_lock(timeout=lock_timeout)
        except asyncio.TimeoutError:
            cleanup_temp_images(image_files)
            self.total_errors += 1
            if image_deadline is not None and image_deadline <= asyncio.get_running_loop().time():
                yield "error", {"message": f"Image request exceeded its {img_timeout:g}s deadline.", "type": BridgeTimeoutError.err_type, "code": BridgeTimeoutError.code, "stage": stage}
                return
            yield "error", {"message": "Bridge đang bận, thử lại sau vài giây.", "type": BridgeBusyError.err_type, "code": BridgeBusyError.code, "stage": stage}
            return

        full_text = ""
        try:
            if turn_preparer is not None:
                prepared = turn_preparer(self)
                if asyncio.iscoroutine(prepared):
                    prepared = await prepared
                prompt_to_send, prepared_images, include_tool_header = prepared
                image_files = list(prepared_images or [])
                if image_files and image_deadline is None:
                    image_deadline = _request_deadline(img_timeout)

            stage = "throttle"
            elapsed = time.time() - self.last_request_ts
            if elapsed < min_interval:
                delay = min_interval - elapsed
                if image_deadline is not None:
                    await asyncio.wait_for(asyncio.sleep(delay), timeout=_remaining_timeout(image_deadline))
                else:
                    await asyncio.sleep(delay)

            if self.chat_session is None:
                self._create_new_chat_session()

            base_prompt = prompt_to_send
            if image_files and not self.vision_upload_available:
                yield "error", {"message": "Gửi ảnh yêu cầu đăng nhập cookie Gemini (không hỗ trợ ở chế độ Guest).", "type": BridgeAuthError.err_type, "code": BridgeAuthError.code, "stage": "vision_upload"}
                return

            attempt, quota_switches, failover_attempts = 0, 0, 0
            max_failover_attempts = max(len(self.chain), 1)
            yielded_any = False
            requested_model = str(self.default_model or "unspecified").strip() or "unspecified"
            fallback_from: Optional[str] = None
            self.last_turn_model = requested_model
            self.last_turn_fallback_from = None
            self.last_turn_prompt_chars = 0
            turn_start = asyncio.get_running_loop().time()
            stream_cap = (
                image_deadline
                if image_deadline is not None
                else (turn_start + stream_overall_timeout)
            )

            while attempt < 2:
                stage = "vision_upload" if image_files else "text_generation"
                saved_meta_before_send = self._sanitize_chat_metadata_before_send()
                prompt_to_send = self._ensure_header_in_prompt(self._apply_pending_handoff(base_prompt), include_tool_header)
                self.last_turn_prompt_chars = len(prompt_to_send)
                now_ts = asyncio.get_running_loop().time()
                initial_deadline = (
                    image_deadline
                    if image_deadline is not None
                    else min(stream_cap, now_ts + hard_timeout)
                )
                soft_fallback_eligible = (
                    attempt == 0
                    and str(self.default_model or "").strip() not in ("", "unspecified")
                    and (initial_deadline - now_ts) > (first_byte_timeout + 10.0)
                )
                first_chunk_deadline = (now_ts + first_byte_timeout) if soft_fallback_eligible else None
                try:
                    if BRIDGE_DEBUG_FAKE_QUOTA and not self._fake_quota_done:
                        self._fake_quota_done = True
                        raise UsageLimitExceededError("DEBUG: giả lập hết quota (BRIDGE_DEBUG_FAKE_QUOTA)")

                    logger.info(f"Stream vào ChatSession (Turn={self.turn_count + 1} | Chars={len(prompt_to_send)}{f' | Images={len(image_files)}' if image_files else ''})")
                    async for output in _iterate_stream_until(
                        self._open_chat_stream(prompt_to_send, image_files),
                        initial_deadline,
                        first_chunk_deadline=first_chunk_deadline,
                        chunk_idle_timeout=chunk_idle_timeout,
                        overall_deadline=stream_cap,
                    ):
                        delta = output.text_delta or ""
                        if delta:
                            yielded_any = True
                            full_text += delta
                            yield "delta", delta
                    self.last_request_ts = time.time()
                    reply = full_text.strip()
                    self.session_initialized = True
                    self._mark_tool_header_sent(include_tool_header)
                    self.turn_count += 1
                    self._consecutive_auth_errors = 0
                    self._consecutive_transient_errors = 0
                    self._pending_handoff = None
                    if fallback_from is not None and hasattr(self.chat_session, "metadata"):
                        self._create_new_chat_session(preserve_metadata=list(self.chat_session.metadata))
                    elif self.turn_count % 15 == 0 and hasattr(self.chat_session, "metadata"):
                        self._create_new_chat_session(preserve_metadata=list(self.chat_session.metadata))
                    self._save_session()
                    self._sync_active_session_slot()
                    yield "done", reply
                    return

                except (asyncio.CancelledError, GeneratorExit):
                    self._restore_chat_metadata_after_abort(saved_meta_before_send)
                    raise

                except asyncio.TimeoutError:
                    retry_margin = min(5.0, max(0.05, stream_overall_timeout * 0.1))
                    if (
                        soft_fallback_eligible
                        and not yielded_any
                        and asyncio.get_running_loop().time() < (stream_cap - retry_margin)
                    ):
                        fallback_model = self._select_fallback_model()
                        fallback_from = requested_model
                        self.last_turn_model = fallback_model
                        self.last_turn_fallback_from = fallback_from
                        logger.warning(
                            f"[MODEL_FALLBACK] Model '{requested_model}' không phản hồi sau {first_byte_timeout:g}s "
                            f"-> Tạm chuyển lượt này sang '{fallback_model}' và nạp lại tool header!"
                        )
                        self.tool_header_sent = False
                        self.chat_tool_header_hash = ""
                        self._restore_chat_metadata_after_abort(saved_meta_before_send, model_override=fallback_model)
                        attempt += 1
                        continue
                    self._restore_chat_metadata_after_abort(saved_meta_before_send)
                    now_end = asyncio.get_running_loop().time()
                    if image_deadline is not None and image_deadline <= now_end:
                        timeout = img_timeout
                    elif now_end >= stream_cap - 0.05:
                        timeout = stream_overall_timeout
                    elif yielded_any:
                        timeout = chunk_idle_timeout
                    else:
                        timeout = hard_timeout
                    logger.error(f"Quá thời gian {timeout:g}s!")
                    self.total_errors += 1
                    yield "error", {"message": f"Gemini không phản hồi sau {timeout:g}s.", "type": BridgeTimeoutError.err_type, "code": BridgeTimeoutError.code, "stage": stage}
                    return

                except UsageLimitExceededError as exc:
                    if yielded_any:
                        yield "error", {"message": f"Hết quota khi đang stream: {exc}", "type": BridgeQuotaError.err_type, "code": BridgeQuotaError.code, "stage": stage}
                        return
                    if quota_switches < len(self.chain):
                        if image_deadline is not None:
                            switched = await _await_with_deadline(self._handle_quota_exhausted(exc), image_deadline)
                        else:
                            task = asyncio.create_task(self._handle_quota_exhausted(exc))
                            while not task.done():
                                await asyncio.wait({task}, timeout=2.5)
                                if not task.done():
                                    yield "delta", ""
                            switched = task.result()
                        if switched:
                            quota_switches += 1
                            continue
                    yield "error", {"message": "Tất cả tài khoản đều hết quota. Hãy thử lại sau hoặc nạp thêm tài khoản.", "type": BridgeQuotaError.err_type, "code": BridgeQuotaError.code, "stage": stage}
                    return

                except Exception as exc:
                    self.total_errors += 1
                    if self._is_auth_error(exc):
                        self._consecutive_auth_errors += 1
                    else:
                        self._consecutive_transient_errors += 1
                    if yielded_any:
                        logger.warning("Stream failed after response began (%s).", type(exc).__name__)
                        err_cls = BridgeAuthError if self._is_auth_error(exc) else BridgeUpstreamError
                        msg = "Gemini authentication failed after streaming began." if err_cls is BridgeAuthError else "Gemini stream failed after response began."
                        if fallback_from is not None:
                            self._restore_chat_metadata_after_abort(saved_meta_before_send)
                        yield "error", {"message": msg, "type": err_cls.err_type, "code": err_cls.code, "stage": stage}
                        return

                    logger.warning(f"Lỗi ChatSession stream (Lần {attempt + 1}/2): {exc}")
                    if "1097" in str(exc) or ("carbon" in str(self.default_model).lower() and not self._is_auth_error(exc)):
                        old_model = str(self.default_model or "unspecified")
                        fallback_target = self._select_fallback_model()
                        logger.warning(f"[MODEL_FALLBACK] Model '{self.default_model}' bị lỗi ({exc}). Tự động fallback sang '{fallback_target}' và mở lại kết nối mới!")
                        try:
                            ok, err_payload, failover_attempts = await self._recover_client_after_error(stage, stream_cap if image_deadline is None else image_deadline, failover_attempts, max_failover_attempts, raise_init_error=False)
                            if err_payload is not None:
                                yield "error", err_payload
                                return
                            if ok:
                                self.default_model = fallback_target
                                self.last_turn_model = fallback_target
                                self.last_turn_fallback_from = old_model
                                self._create_new_chat_session(preserve_metadata=None)
                                self._save_session()
                                attempt += 1
                                continue
                        except Exception as fb_exc:
                            logger.error(f"[MODEL_FALLBACK] Lỗi khi fallback: {fb_exc}")

                    if self._consecutive_auth_errors >= 2 and not self.is_guest:
                        logger.critical("⚠️ COOKIE CÓ THỂ HẾT HẠN! Cập nhật cookie trong .env hoặc gọi POST /v1/update-cookies")
                        if failover_attempts < max_failover_attempts and len(self.chain) > 1 and await _await_with_deadline(self._try_failover_to_backup(), image_deadline):
                            failover_attempts += 1
                            continue

                    if attempt == 0:
                        saved_meta = self._chat_metadata_snapshot()
                        ok, err_payload, failover_attempts = await self._recover_client_after_error(stage, image_deadline, failover_attempts, max_failover_attempts, raise_init_error=True)
                        if err_payload is not None:
                            yield "error", err_payload
                            return
                        if ok:
                            self._create_new_chat_session(preserve_metadata=saved_meta)
                            full_text = ""
                            attempt += 1
                    else:
                        if fallback_from is not None:
                            self._restore_chat_metadata_after_abort(saved_meta_before_send)
                        yield "error", {"message": f"Lỗi kết nối Gemini Web: {exc}", "type": BridgeUpstreamError.err_type, "code": BridgeUpstreamError.code, "stage": stage}
                        return

            if fallback_from is not None:
                self._restore_chat_metadata_after_abort(saved_meta_before_send)
            yield "error", {"message": "Không nhận được phản hồi từ Gemini.", "type": BridgeUpstreamError.err_type, "code": BridgeUpstreamError.code, "stage": stage}
        except asyncio.TimeoutError:
            if image_deadline is None:
                raise
            self.total_errors += 1
            yield "error", {"message": f"Image request exceeded its {img_timeout:g}s deadline.", "type": BridgeTimeoutError.err_type, "code": BridgeTimeoutError.code, "stage": stage}
        finally:
            self.release_turn_lock(acquired_here)
            cleanup_temp_images(image_files)
