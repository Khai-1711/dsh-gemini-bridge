"""Quản lý xác thực, chuỗi tài khoản, cookie hot-reload và failover có trần."""
import asyncio
import inspect
import sys
import time
from typing import List, Optional

from gemini_webapi.constants import AccountStatus
from gemini_webapi.exceptions import AuthError

from app.accounts import (
    account_key,
    build_account_chain,
    find_chain_overlap,
    first_available_index,
    is_available,
    load_state,
    mark_exhausted,
    next_available_index,
    save_state,
    write_chain_file,
)
from app.config import (
    AUTH_COOLDOWN_MINUTES,
    BASE_DIR,
    GEMINI_HARD_TIMEOUT,
    GEMINI_STREAM_OVERALL_TIMEOUT,
    INSTANCE_ID,
    INSTANCE_LABEL,
    LOCK_ACQUIRE_TIMEOUT,
    PORT,
    logger,
)
from app.cookie_sync import (
    persist_cookie_jar_to_lane_cache,
    persist_cookies_to_lane_env,
    try_auto_sync_for_worker_slot,
)
from app.errors import BridgeControlError
from app.session_pool import clear_saved_session


def _init_supports_kwarg(fn, name: str) -> bool:
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return False
    return any(
        p.kind == inspect.Parameter.VAR_KEYWORD or p.name == name
        for p in sig.parameters.values()
    )


async def _safe_init_client(
    client,
    *,
    timeout: float = 25.0,
    request_timeout: Optional[float] = None,
    close_delay: float = 450,
    auto_refresh: bool = True,
) -> None:
    """Bọc `client.init(...)` với `asyncio.wait_for` để chặn bug deadlock `self._lock`
    của `gemini_webapi` khi `batchexecute` trả 400 (vd cookie bị redirect về ServiceLogin).

    Lưu ý quan trọng: `timeout` và `watchdog_timeout` trong `client.init(...)` được dùng làm
    HTTP read timeout & thinking stall threshold cho MỌI lượt stream về sau của `gemini_webapi`,
    nên luôn giữ đủ dài (>= 420s / >= 180s) để model thinking/viết code dài không bị cắt ngang.
    """
    stream_timeout = (
        float(request_timeout)
        if request_timeout is not None
        else float(max(420, GEMINI_STREAM_OVERALL_TIMEOUT, GEMINI_HARD_TIMEOUT))
    )
    init_kwargs = {
        "timeout": stream_timeout,
        "auto_close": False,
        "close_delay": close_delay,
        "auto_refresh": auto_refresh,
    }
    if _init_supports_kwarg(client.init, "watchdog_timeout"):
        init_kwargs["watchdog_timeout"] = float(max(180, GEMINI_HARD_TIMEOUT))
    try:
        await asyncio.wait_for(
            client.init(**init_kwargs),
            timeout=max(0.05, timeout),
        )
    except asyncio.TimeoutError as exc:
        # Đóng trực tiếp AsyncSession bên dưới (không đụng client._lock nếu đang bị kẹt)
        raw_http = getattr(client, "client", None)
        if raw_http is not None:
            try:
                await asyncio.wait_for(raw_http.close(), timeout=2.0)
            except Exception:
                pass
            try:
                client.client = None
                client._running = False
            except Exception:
                pass
        raise AuthError(
            "Khởi tạo Gemini Client quá thời gian (cookie hết hạn hoặc thiếu OSID/phiên Google)."
        ) from exc


def _gs():
    return sys.modules["app.gemini_session"]


class AccountPoolMixin:
    """Mixin xử lý vòng đời cookie, kiểm tra đăng nhập và chuyển tài khoản dự phòng."""

    def _guest_mode_permitted(self) -> bool:
        """Chỉ cho phép chạy Guest Mode khi người dùng bật ALLOW_GUEST_MODE và KHÔNG cấu hình cookie thật.

        Khi đã nạp cookie tài khoản (`len(self.chain) > 0`), việc rơi về UNAUTHENTICATED/Guest
        phải bị báo mất xác thực để kích hoạt failover sang tài khoản/worker khỏe.
        """
        return bool(self.allow_guest and self.is_guest and not self.chain)

    def _is_auth_error(self, exc: BaseException) -> bool:
        if isinstance(exc, AuthError):
            return True
        text = str(exc).lower()
        if any(k in text for k in ("permission denied", "unauthenticated", "cookie")):
            return True
        if self._guest_mode_permitted():
            return False
        status = getattr(self.client, "account_status", None)
        source = str(getattr(self.client, "_cookie_source", "") or "").lower()
        return status != AccountStatus.AVAILABLE or source == "guest"

    @property
    def account_key(self) -> str:
        return account_key(self._active_psid)

    def _evaluate_auth(self) -> bool:
        status = getattr(self.client, "account_status", None)
        source = str(getattr(self.client, "_cookie_source", "") or "")
        status_name = getattr(status, "name", None) or str(status or "UNKNOWN")

        self.account_status_name = status_name
        self.cookie_source = source
        self.is_guest = status != AccountStatus.AVAILABLE or source.lower() == "guest"
        self.auth_ok = not self.is_guest

        if self.is_guest:
            if self._guest_mode_permitted():
                logger.warning(
                    f"[AUTH] GeminiClient ở chế độ KHÁCH (account_status={status_name}, "
                    f"cookie_source={source}). Chạy ở Guest Mode không cookie (ALLOW_GUEST_MODE=true)."
                )
            else:
                logger.warning(
                    f"[AUTH] GeminiClient mất xác thực / bị tụt về KHÁCH "
                    f"(account_status={status_name}, cookie_source={source}). "
                    "Không tự chạy Guest Mode vì luồng đã cấu hình cookie thật."
                )
        else:
            logger.info(
                f"[AUTH] Đã đăng nhập Gemini Web (account_status={status_name}, "
                f"cookie_source={source})."
            )
        return self.auth_ok

    def _refresh_auth_flags_quietly(self) -> None:
        status = getattr(self.client, "account_status", None)
        source = str(getattr(self.client, "_cookie_source", "") or "")
        status_name = getattr(status, "name", None) or str(status or "UNKNOWN")
        self.account_status_name = status_name
        self.cookie_source = source
        self.is_guest = status != AccountStatus.AVAILABLE or source.lower() == "guest"
        self.auth_ok = not self.is_guest

    async def _try_auto_cookie_sync_locked(self) -> bool:
        """Tự động chạy headless sync từ Profile Vault nếu luồng/ô hiện tại có profile đã đăng nhập."""
        lane_id = INSTANCE_ID or "default"
        slot = self.active_index if 0 <= self.active_index <= 4 else 0
        try:
            sync_res = await try_auto_sync_for_worker_slot(lane_id, slot)
        except Exception as exc:
            logger.warning("[COOKIE-SYNC] Lỗi khi thử tự đồng bộ profile: %s", exc)
            return False
        if sync_res is None or not sync_res.ok:
            return False

        client_cls = _gs().GeminiClient
        paths = _gs().INSTANCE_PATHS
        if getattr(sync_res, "cookies", None):
            persist_cookie_jar_to_lane_cache(BASE_DIR, lane_id, sync_res.psid, sync_res.cookies)
        candidate = client_cls(sync_res.psid, sync_res.psidts)
        try:
            await _safe_init_client(candidate, timeout=30.0)
            cand_status = getattr(candidate, "account_status", None)
            cand_source = str(getattr(candidate, "_cookie_source", "") or "")
            cand_ok = cand_status == AccountStatus.AVAILABLE and cand_source.lower() != "guest"
            if not cand_ok:
                await candidate.close()
                logger.warning(
                    "[COOKIE-SYNC] Cookie vừa sync cho '%s' (slot %s) không xác thực được (%s).",
                    lane_id,
                    slot,
                    getattr(cand_status, "name", cand_status),
                )
                return False
        except Exception as exc:
            try:
                await candidate.close()
            except Exception:
                pass
            logger.warning("[COOKIE-SYNC] Xác thực cookie vừa sync thất bại: %s", exc)
            return False

        old_client = self.client
        self.client = candidate
        try:
            await old_client.close()
        except Exception:
            pass

        self._active_psid = sync_res.psid
        new_key = account_key(sync_res.psid)
        if not self.chain:
            self.chain = [
                {
                    "label": INSTANCE_LABEL or "chinh",
                    "psid": sync_res.psid,
                    "psidts": sync_res.psidts,
                    "key": new_key,
                }
            ]
            self.active_index = 0
        elif 0 <= slot < len(self.chain):
            self.chain[slot]["psid"] = sync_res.psid
            self.chain[slot]["psidts"] = sync_res.psidts
            self.chain[slot]["key"] = new_key

        if new_key in self.account_state:
            self.account_state.pop(new_key, None)
            save_state(paths["accounts_state_file"], self.account_state)

        self._evaluate_auth()
        self._refresh_available_models()
        self._ensure_default_model()
        self._ensure_session_pool().clear()
        self._active_slot_model = self.default_model
        saved = self._chat_metadata_snapshot()
        self._create_new_chat_session(preserve_metadata=saved)
        self._sync_active_session_slot()
        self._consecutive_auth_errors = 0

        persisted, env_name, err = persist_cookies_to_lane_env(
            BASE_DIR, lane_id, slot, sync_res.psid, sync_res.psidts
        )
        if persisted:
            logger.info(
                "[COOKIE-SYNC] Đã tự phục hồi & lưu cookie mới vào %s (account=%s).",
                env_name,
                new_key,
            )
        else:
            logger.warning(
                "[COOKIE-SYNC] Đã tự phục hồi trong bộ nhớ nhưng không ghi được %s: %s",
                env_name,
                err,
            )
        return bool(self.auth_ok)

    async def ensure_authenticated(self, force: bool = False) -> bool:
        """Trả True nếu đang đăng nhập (hoặc Guest không cookie hợp lệ).

        Nếu `force=True` (bấm Reauth từ Dashboard), bỏ qua bộ đệm 60s và bắt buộc init lại client.
        """
        if self.state == "off":
            return False
        if not force:
            self._refresh_auth_flags_quietly()
            if self.auth_ok or self._guest_mode_permitted():
                return True

            now = time.time()
            if now - self._last_recover_ts < 60:
                return False
        self._last_recover_ts = time.time()

        acquired_here = await self.acquire_turn_lock(LOCK_ACQUIRE_TIMEOUT)
        try:
            logger.warning(
                "[AUTH] Đang phục hồi đăng nhập (init lại client%s)...",
                " - ép thủ công" if force else "",
            )
            init_failed = False
            try:
                try:
                    await self.client.close()
                except Exception:
                    pass
                await _safe_init_client(self.client, timeout=30.0)
                self._refresh_available_models()
                self._ensure_default_model()
            except Exception as exc:
                logger.error(f"[AUTH] Init lại thất bại: {exc}")
                init_failed = True
            if not init_failed and self._evaluate_auth():
                saved = self._chat_metadata_snapshot()
                self._create_new_chat_session(preserve_metadata=saved)
                self._sync_active_session_slot()
                return True
            if await self._try_auto_cookie_sync_locked():
                return True
            if len(self.chain) > 1 and await self._try_failover_to_backup():
                return True
            return False if init_failed else bool(self._guest_mode_permitted())
        finally:
            self.release_turn_lock(acquired_here)

    async def update_cookies(self, new_psid: str, new_psidts: str) -> dict:
        """Hot-reload cookie mà không cần restart server (xác thực trước, lỗi thì rollback nguyên vẹn)."""
        if self.state in ("starting", "stopping"):
            raise BridgeControlError(
                "Đang có thao tác bật/tắt, không thể cập nhật cookie lúc này."
            )
        client_cls = _gs().GeminiClient
        paths = _gs().INSTANCE_PATHS

        acquired_here = await self.acquire_turn_lock(LOCK_ACQUIRE_TIMEOUT)
        try:
            if self.state == "off":
                self._active_psid = new_psid
                self.client = client_cls(new_psid, new_psidts)
                if self.chain:
                    self.chain[0]["psid"] = new_psid
                    self.chain[0]["psidts"] = new_psidts
                    self.chain[0]["key"] = account_key(new_psid)
                logger.info("[CONTROL] Đã lưu cookie mới; sẽ dùng khi bật bridge.")
                return {"status": "saved_while_off"}

            candidate = client_cls(new_psid, new_psidts)
            try:
                await _safe_init_client(candidate, timeout=30.0)
                cand_status = getattr(candidate, "account_status", None)
                cand_source = str(getattr(candidate, "_cookie_source", "") or "")
                cand_ok = cand_status == AccountStatus.AVAILABLE and cand_source.lower() != "guest"
                if not cand_ok:
                    raise AuthError("Cookie mới không đăng nhập được (guest/hết hạn)")
            except Exception:
                try:
                    await candidate.close()
                except Exception:
                    pass
                raise

            old_client = self.client
            self.client = candidate
            try:
                await old_client.close()
            except Exception:
                pass

            self._active_psid = new_psid
            new_key = account_key(new_psid)
            if self.chain:
                target_slot = self.active_index if 0 <= self.active_index < len(self.chain) else 0
                self.chain[target_slot]["psid"] = new_psid
                self.chain[target_slot]["psidts"] = new_psidts
                self.chain[target_slot]["key"] = new_key
            if new_key in self.account_state:
                self.account_state.pop(new_key, None)
                save_state(paths["accounts_state_file"], self.account_state)

            self._evaluate_auth()
            self._refresh_available_models()
            self._ensure_default_model()
            self._ensure_session_pool().clear()
            self._active_slot_model = self.default_model
            self._create_new_chat_session()
            self._sync_active_session_slot()
            clear_saved_session()
            self._consecutive_auth_errors = 0
            self._using_backup = False
            logger.info("✅ Cookie đã được cập nhật thành công!")
            return {
                "status": "ok",
                "message": "Cookies updated successfully",
                "model": self.default_model,
            }
        finally:
            self.release_turn_lock(acquired_here)

    async def update_slot_cookie(
        self,
        slot: int,
        psid: str,
        psidts: str,
        label: str = "",
        cookies: Optional[List[dict]] = None,
    ) -> dict:
        """Cập nhật cookie cho ô tài khoản chỉ định (slot 0..4), rollback an toàn nếu cookie lỗi."""
        if self.state in ("starting", "stopping"):
            raise BridgeControlError(
                "Đang có thao tác bật/tắt, không thể cập nhật cookie lúc này."
            )
        if slot < 0 or slot > 4:
            raise ValueError("Slot phải từ 0 đến 4.")

        new_key = account_key(psid)
        for idx, acc in enumerate(self.chain or []):
            if idx != slot and acc.get("key") == new_key:
                raise ValueError(f"Cookie trùng với ô tài khoản slot {idx} trong luồng này.")

        paths = _gs().INSTANCE_PATHS
        client_cls = _gs().GeminiClient
        acquired_here = await self.acquire_turn_lock(LOCK_ACQUIRE_TIMEOUT)
        try:
            if slot > len(self.chain):
                raise ValueError(f"Chưa có slot {slot - 1}. Hãy nạp các slot theo thứ tự liên tục.")

            if cookies:
                persist_cookie_jar_to_lane_cache(
                    BASE_DIR, INSTANCE_ID or "default", psid, cookies
                )

            # Xác thực trước trên client tạm nếu cập nhật slot đang chạy HOẶC luồng đang mất auth
            should_activate = slot == self.active_index or not self.auth_ok
            validated_client = None
            if should_activate and self.state != "off":
                candidate = client_cls(psid, psidts)
                try:
                    await _safe_init_client(candidate, timeout=25.0)
                    cand_status = getattr(candidate, "account_status", None)
                    cand_source = str(getattr(candidate, "_cookie_source", "") or "")
                    cand_ok = cand_status == AccountStatus.AVAILABLE and cand_source.lower() != "guest"
                    if not cand_ok:
                        raise AuthError(
                            "Cookie mới không đăng nhập được (guest mode hoặc cookie hết hạn)"
                        )
                    validated_client = candidate
                except Exception:
                    try:
                        await candidate.close()
                    except Exception:
                        pass
                    raise

            if slot == len(self.chain):
                default_lbl = f"backup{slot}" if slot > 0 else (INSTANCE_LABEL or "chinh")
                self.chain.append(
                    {"label": label or default_lbl, "psid": psid, "psidts": psidts, "key": new_key}
                )
            else:
                if label:
                    self.chain[slot]["label"] = label
                self.chain[slot]["psid"] = psid
                self.chain[slot]["psidts"] = psidts
                self.chain[slot]["key"] = new_key

            if should_activate:
                self.active_index = slot
                self._using_backup = slot > 0
                if self.state == "off":
                    self._active_psid = psid
                    self.client = client_cls(psid, psidts)
                    logger.info(
                        f"[CONTROL] Đã lưu cookie mới cho slot {slot} (đang tắt); sẽ dùng khi bật bridge."
                    )
                else:
                    old_client = self.client
                    self.client = validated_client
                    try:
                        await old_client.close()
                    except Exception:
                        pass
                    self._active_psid = psid
                    self._evaluate_auth()
                    self._refresh_available_models()
                    self._ensure_default_model()
                    self._ensure_session_pool().clear()
                    self._active_slot_model = self.default_model
                    self._create_new_chat_session()
                    self._sync_active_session_slot()
                    clear_saved_session()
                    self._consecutive_auth_errors = 0
                    logger.info(f"✅ Cookie slot {slot} đã được cập nhật thành công!")

            if new_key in self.account_state:
                self.account_state.pop(new_key, None)
                save_state(paths["accounts_state_file"], self.account_state)

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
                    logger.warning(
                        f"[ACCOUNTS] Tài khoản mới nạp ở slot {slot} trùng với luồng '{x.get('other_instance')}'."
                    )
            except Exception as exc:
                logger.warning(f"[ACCOUNTS] Lỗi ghi chain file sau khi nạp cookie: {exc}")

            auth_status = (
                self.auth_ok if (slot == self.active_index and self.state == "on") else True
            )
            return {"ok": True, "auth_ok": auth_status, "account_key": new_key}
        finally:
            self.release_turn_lock(acquired_here)

    def mark_current_exhausted(self, reason: str, hours: float) -> None:
        self.mark_exhausted_for(self.active_index, reason, hours)

    def mark_exhausted_for(self, index: int, reason: str, hours: float) -> None:
        if not self.chain or index >= len(self.chain):
            return
        key = self.chain[index]["key"]
        mark_exhausted(
            self.account_state, key, time.time() + hours * 3600, reason, time.time()
        )
        save_state(_gs().INSTANCE_PATHS["accounts_state_file"], self.account_state)

    async def _switch_to_account(self, index: int) -> bool:
        acc = self.chain[index]
        client_cls = _gs().GeminiClient
        try:
            await self.client.close()
        except Exception:
            pass
        self.client = client_cls(acc["psid"], acc["psidts"])
        try:
            await _safe_init_client(self.client, timeout=30.0)
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
        self._active_slot_model = self.default_model
        self._create_new_chat_session()
        self._ensure_session_pool().clear()
        self._sync_active_session_slot()
        self._consecutive_auth_errors = 0
        self._consecutive_transient_errors = 0
        self._using_backup = index > 0
        return True

    async def _rebuild_client(self, index: int) -> None:
        if not self.chain or index >= len(self.chain):
            return
        acc = self.chain[index]
        client_cls = _gs().GeminiClient
        try:
            await self.client.close()
        except Exception:
            pass
        self.client = client_cls(acc["psid"], acc["psidts"])
        self._active_psid = acc["psid"]
        self.active_index = index
        try:
            await _safe_init_client(self.client, timeout=30.0)
        except Exception as exc:
            logger.error(f"[QUOTA] Không dựng lại được client '{acc['label']}': {exc}")
        self._evaluate_auth()

    async def _try_failover_to_backup(self) -> bool:
        if self.state == "off" or not self.chain or len(self.chain) <= 1:
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
                logger.warning(f"[QUOTA] Đã chuyển sang tài khoản '{self.chain[idx]['label']}'.")
                return True


__all__ = [
    "AccountPoolMixin",
    "account_key",
    "build_account_chain",
    "find_chain_overlap",
    "first_available_index",
    "is_available",
    "load_state",
    "mark_exhausted",
    "next_available_index",
    "save_state",
    "write_chain_file",
]
