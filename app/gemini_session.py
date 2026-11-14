import asyncio
import hashlib
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from gemini_webapi import GeminiClient

from app.config import (
    BACKUP_1PSID,
    BACKUP_1PSIDTS,
    BASE_DIR,
    GEMINI_HARD_TIMEOUT,
    LOCK_ACQUIRE_TIMEOUT,
    MIN_REQUEST_INTERVAL,
    SECURE_1PSID,
    SECURE_1PSIDTS,
    logger,
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
SESSION_STATE_FILE = BASE_DIR / ".bridge_session.json"


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


def save_session_state(metadata: list, model: str) -> None:
    """Lưu metadata phiên chat để resume sau khi restart."""
    try:
        SESSION_STATE_FILE.write_text(
            json.dumps(
                {
                    "metadata": [m for m in metadata],
                    "model": model,
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
        self.client = GeminiClient(SECURE_1PSID, SECURE_1PSIDTS)
        self.lock = asyncio.Lock()
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
        self.vision_upload_available = True
        self._consecutive_auth_errors = 0
        self._using_backup = False

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
        saved = load_saved_session()
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
        if self.chat_session is None:
            return
        try:
            meta = list(self.chat_session.metadata)
        except Exception:
            return
        if meta and meta[0]:
            save_session_state(meta, self.default_model)

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

    async def update_cookies(self, new_psid: str, new_psidts: str) -> None:
        """Hot-reload cookie mà không cần restart server."""
        async with self.lock:
            try:
                await self.client.close()
            except Exception:
                pass
            self.client = GeminiClient(new_psid, new_psidts)
            await self.client.init(
                timeout=90, auto_close=False, close_delay=300, auto_refresh=True
            )
            self._refresh_available_models()
            self._ensure_default_model()
            self._create_new_chat_session()
            clear_saved_session()
            self.vision_upload_available = True
            self._consecutive_auth_errors = 0
            self._using_backup = False
            logger.info("✅ Cookie đã được cập nhật thành công!")

    async def _try_failover_to_backup(self) -> bool:
        """Chuyển sang cookie dự phòng nếu có."""
        if self._using_backup or not BACKUP_1PSID or not BACKUP_1PSIDTS:
            return False

        logger.warning("⚠️ Đang chuyển sang tài khoản Cookie dự phòng...")
        try:
            await self.client.close()
        except Exception:
            pass

        try:
            self.client = GeminiClient(BACKUP_1PSID, BACKUP_1PSIDTS)
            await self.client.init(
                timeout=90, auto_close=False, close_delay=300, auto_refresh=True
            )
            self._refresh_available_models()
            self._ensure_default_model()
            self._create_new_chat_session()
            self._using_backup = True
            self._consecutive_auth_errors = 0
            self.vision_upload_available = True
            logger.info("✅ Đã chuyển sang tài khoản dự phòng thành công!")
            return True
        except Exception as e:
            logger.error(f"❌ Tài khoản dự phòng cũng lỗi: {e}")
            return False

    async def reset_session(self) -> None:
        async with self.lock:
            self._create_new_chat_session()
            clear_saved_session()
            self.vision_upload_available = True
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
        return {
            "status": "ok",
            "model": self.default_model,
            "available_models": self.available_models,
            "model_details": self.model_details,
            "session_initialized": self.session_initialized,
            "turn_count": self.turn_count,
            "total_requests": self.total_requests,
            "total_errors": self.total_errors,
            "error_rate": f"{(self.total_errors / max(self.total_requests, 1)) * 100:.1f}%",
            "uptime": f"{hours}h {minutes}m {seconds}s",
            "using_backup_cookie": self._using_backup,
            "vision_upload_available": self.vision_upload_available,
            "cache_entries": len(_response_cache),
        }

    async def send_prepared_turn(
        self, prompt_to_send: str, image_files: List[str]
    ) -> str:
        """Gửi 1 lượt hội thoại vào Gemini Web với đầy đủ cơ chế bảo vệ."""
        self.total_requests += 1

        # Kiểm tra cache (chỉ cho prompt không có ảnh)
        if not image_files:
            cached = get_cached_response(prompt_to_send, self.default_model)
            if cached:
                return cached

        # Lock với timeout — trả lỗi thay vì chờ vô hạn (M2)
        try:
            await asyncio.wait_for(self.lock.acquire(), timeout=LOCK_ACQUIRE_TIMEOUT)
        except asyncio.TimeoutError:
            cleanup_temp_images(image_files)
            self.total_errors += 1
            return (
                "[SERVER BUSY] Gemini đang xử lý yêu cầu khác, vui lòng thử lại sau vài giây."
            )

        try:
            # Giãn cách an toàn
            elapsed = time.time() - self.last_request_ts
            if elapsed < MIN_REQUEST_INTERVAL:
                await asyncio.sleep(MIN_REQUEST_INTERVAL - elapsed)

            if self.chat_session is None:
                self._create_new_chat_session()

            # ─────────────────────────────────────────────────────
            # TẦNG 1: Gửi ảnh trực tiếp lên Gemini (nếu có ảnh)
            # ─────────────────────────────────────────────────────
            if image_files and self.vision_upload_available:
                try:
                    logger.info(
                        f"[Vision Layer 1] Upload {len(image_files)} ảnh lên Gemini..."
                    )
                    coro = self.chat_session.send_message(
                        prompt_to_send, files=image_files
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
                    self._save_session()
                    return reply
                except Exception as img_exc:
                    logger.warning(
                        f"Upload ảnh thất bại ({img_exc}). --> Chuyển sang Local OCR!"
                    )
                    self.vision_upload_available = False

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
                prompt_to_send = (
                    "\n\n".join(ocr_blocks)
                    + "\n\n(Above is text extracted from the user's screenshot. "
                    "Analyze and respond in the SAME LANGUAGE as the user.)\n\n"
                    + prompt_to_send
                )

            # ─────────────────────────────────────────────────────
            # TẦNG 3: Gửi qua cổng Text (luôn sống với 1PSID)
            # ─────────────────────────────────────────────────────
            for attempt in range(2):
                try:
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
                    self._save_session()

                    # Cache kết quả (chỉ text response, không cache tool calls)
                    if not image_files and "<tool_call>" not in reply:
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
                    return "Yêu cầu xử lý quá lâu, vui lòng thử lại."

                except Exception as exc:
                    self.total_errors += 1
                    self._consecutive_auth_errors += 1
                    logger.warning(
                        f"Lỗi ChatSession (Lần {attempt + 1}/2): {exc}"
                    )

                    # Cảnh báo cookie hết hạn
                    if self._consecutive_auth_errors >= 3:
                        logger.critical(
                            "⚠️ COOKIE CÓ THỂ HẾT HẠN! "
                            "Cập nhật cookie trong .env hoặc gọi POST /v1/update-cookies"
                        )
                        if await self._try_failover_to_backup():
                            # Thử lại với tài khoản backup
                            continue

                    if attempt == 0:
                        try:
                            await self.client.close()
                        except Exception:
                            pass
                        await self.client.init(
                            timeout=60,
                            auto_close=False,
                            close_delay=300,
                            auto_refresh=True,
                        )
                        self._create_new_chat_session()
                    else:
                        return f"Lỗi kết nối Gemini Web: {exc}"

            return "Không nhận được phản hồi."
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
        self.total_requests += 1

        try:
            await asyncio.wait_for(self.lock.acquire(), timeout=LOCK_ACQUIRE_TIMEOUT)
        except asyncio.TimeoutError:
            cleanup_temp_images(image_files)
            self.total_errors += 1
            msg = (
                "[SERVER BUSY] Gemini đang xử lý yêu cầu khác, "
                "vui lòng thử lại sau vài giây."
            )
            yield "text", msg
            yield "done", msg
            return

        full_text = ""
        try:
            elapsed = time.time() - self.last_request_ts
            if elapsed < MIN_REQUEST_INTERVAL:
                await asyncio.sleep(MIN_REQUEST_INTERVAL - elapsed)

            if self.chat_session is None:
                self._create_new_chat_session()

            # Tầng 1: stream kèm ảnh trực tiếp (nếu có)
            if image_files and self.vision_upload_available:
                try:
                    logger.info(
                        f"[Vision Layer 1] Stream {len(image_files)} ảnh lên Gemini..."
                    )
                    async with _asyncio_timeout(GEMINI_HARD_TIMEOUT):
                        async for output in self.chat_session.send_message_stream(
                            prompt_to_send, files=image_files
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
                    self._save_session()
                    yield "done", full_text.strip()
                    return
                except asyncio.TimeoutError:
                    logger.error(f"[Vision] Quá thời gian {GEMINI_HARD_TIMEOUT}s!")
                    self.total_errors += 1
                    self.vision_upload_available = False
                    full_text = ""
                except Exception as img_exc:
                    logger.warning(
                        f"Stream ảnh thất bại ({img_exc}). --> Chuyển sang Local OCR!"
                    )
                    self.vision_upload_available = False
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
                prompt_to_send = (
                    "\n\n".join(ocr_blocks)
                    + "\n\n(Above is text extracted from the user's screenshot. "
                    "Analyze and respond in the SAME LANGUAGE as the user.)\n\n"
                    + prompt_to_send
                )

            # Tầng 3: stream text
            for attempt in range(2):
                try:
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
                                full_text += delta
                                yield "delta", delta
                    self.last_request_ts = time.time()
                    reply = full_text.strip()
                    self.session_initialized = True
                    self.tool_header_sent = True
                    self.turn_count += 1
                    self._consecutive_auth_errors = 0
                    self._save_session()

                    if not image_files and "<tool_call>" not in reply:
                        set_cached_response(prompt_to_send, reply, self.default_model)

                    if self.turn_count % 15 == 0 and hasattr(self.chat_session, "metadata"):
                        saved_meta = list(self.chat_session.metadata)
                        self._create_new_chat_session(preserve_metadata=saved_meta)

                    yield "done", reply
                    return

                except asyncio.TimeoutError:
                    logger.error(f"Quá thời gian {GEMINI_HARD_TIMEOUT}s!")
                    self.total_errors += 1
                    msg = "Yêu cầu xử lý quá lâu, vui lòng thử lại."
                    yield "text", msg
                    yield "done", msg
                    return

                except Exception as exc:
                    self.total_errors += 1
                    self._consecutive_auth_errors += 1
                    logger.warning(
                        f"Lỗi ChatSession stream (Lần {attempt + 1}/2): {exc}"
                    )
                    if self._consecutive_auth_errors >= 3:
                        logger.critical(
                            "⚠️ COOKIE CÓ THỂ HẾT HẠN! "
                            "Cập nhật cookie trong .env hoặc gọi POST /v1/update-cookies"
                        )
                        if await self._try_failover_to_backup():
                            continue
                    if attempt == 0:
                        try:
                            await self.client.close()
                        except Exception:
                            pass
                        await self.client.init(
                            timeout=60, auto_close=False, close_delay=300, auto_refresh=True
                        )
                        self._create_new_chat_session()
                        full_text = ""
                    else:
                        msg = f"Lỗi kết nối Gemini Web: {exc}"
                        yield "text", msg
                        yield "done", msg
                        return

            yield "done", full_text.strip()
        finally:
            self.lock.release()
            cleanup_temp_images(image_files)


# Singleton instance
session_manager = SingleChatManager()
