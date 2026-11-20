"""Quản lý xác thực, chuỗi tài khoản, cookie hot-reload và failover có trần."""
import sys
import time

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
    INSTANCE_ID,
    INSTANCE_LABEL,
    PORT,
    logger,
)
from app.errors import BridgeControlError
from app.session_pool import clear_saved_session


def _gs():
    return sys.modules["app.gemini_session"]


class AccountPoolMixin:
    """Mixin xử lý vòng đời cookie, kiểm tra đăng nhập và chuyển tài khoản dự phòng."""

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
            if self.allow_guest:
                logger.warning(
                    f"[AUTH] GeminiClient ở chế độ KHÁCH (account_status={status_name}, "
                    f"cookie_source={source}). Chạy ở Guest Mode (cho phép bởi ALLOW_GUEST_MODE=true)."
                )
            else:
                logger.warning(
                    f"[AUTH] GeminiClient ở chế độ KHÁCH / CHƯA ĐĂNG NHẬP "
                    f"(account_status={status_name}, cookie_source={source})."
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

    async def ensure_authenticated(self) -> bool:
        """Trả True nếu đang đăng nhập hoặc Guest hợp lệ; ngược lại thử phục hồi tối đa 1 lần/60s."""
        if self.state == "off":
            return False
        self._refresh_auth_flags_quietly()
        if self.auth_ok or (self.allow_guest and self.is_guest):
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
            if len(self.chain) > 1 and await self._try_failover_to_backup():
                return True
            return bool(self.allow_guest and self.is_guest)

    async def update_cookies(self, new_psid: str, new_psidts: str) -> dict:
        """Hot-reload cookie mà không cần restart server."""
        if self.state in ("starting", "stopping"):
            raise BridgeControlError(
                "Đang có thao tác bật/tắt, không thể cập nhật cookie lúc này."
            )
        client_cls = _gs().GeminiClient

        async with self.lock:
            if self.state == "off":
                self._active_psid = new_psid
                self.client = client_cls(new_psid, new_psidts)
                if self.chain:
                    self.chain[0]["psid"] = new_psid
                    self.chain[0]["psidts"] = new_psidts
                    self.chain[0]["key"] = account_key(new_psid)
                logger.info("[CONTROL] Đã lưu cookie mới; sẽ dùng khi bật bridge.")
                return {"status": "saved_while_off"}

            try:
                await self.client.close()
            except Exception:
                pass
            self.client = client_cls(new_psid, new_psidts)
            await self.client.init(
                timeout=90, auto_close=False, close_delay=300, auto_refresh=True
            )
            self._active_psid = new_psid
            if self.chain:
                self.chain[0]["psid"] = new_psid
                self.chain[0]["psidts"] = new_psidts
                self.chain[0]["key"] = account_key(new_psid)
            ok = self._evaluate_auth()
            self._refresh_available_models()
            self._ensure_default_model()
            self._create_new_chat_session()
            clear_saved_session()
            self._consecutive_auth_errors = 0
            self._using_backup = False
            if not ok:
                raise AuthError("Cookie mới không đăng nhập được (guest/hết hạn)")
            logger.info("✅ Cookie đã được cập nhật thành công!")
            return {
                "status": "ok",
                "message": "Cookies updated successfully",
                "model": self.default_model,
            }

    async def update_slot_cookie(
        self, slot: int, psid: str, psidts: str, label: str = ""
    ) -> dict:
        """Cập nhật cookie cho ô tài khoản chỉ định (slot 0..4)."""
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
        async with self.lock:
            if slot == len(self.chain):
                default_lbl = f"backup{slot}" if slot > 0 else (INSTANCE_LABEL or "chinh")
                self.chain.append(
                    {"label": label or default_lbl, "psid": psid, "psidts": psidts, "key": new_key}
                )
            elif 0 <= slot < len(self.chain):
                if label:
                    self.chain[slot]["label"] = label
                self.chain[slot]["psid"] = psid
                self.chain[slot]["psidts"] = psidts
                self.chain[slot]["key"] = new_key
            else:
                raise ValueError(f"Chưa có slot {slot - 1}. Hãy nạp các slot theo thứ tự liên tục.")

            if slot == self.active_index:
                if self.state == "off":
                    self._active_psid = psid
                    self.client = client_cls(psid, psidts)
                    logger.info(
                        f"[CONTROL] Đã lưu cookie mới cho slot {slot} (đang tắt); sẽ dùng khi bật bridge."
                    )
                else:
                    try:
                        await self.client.close()
                    except Exception:
                        pass
                    self.client = client_cls(psid, psidts)
                    await self.client.init(
                        timeout=90, auto_close=False, close_delay=300, auto_refresh=True
                    )
                    self._active_psid = psid
                    ok = self._evaluate_auth()
                    self._refresh_available_models()
                    self._ensure_default_model()
                    self._create_new_chat_session()
                    clear_saved_session()
                    self._consecutive_auth_errors = 0
                    if not ok:
                        raise AuthError(
                            "Cookie mới không đăng nhập được (guest mode hoặc cookie hết hạn)"
                        )
                    logger.info(f"✅ Cookie slot {slot} đã được cập nhật thành công!")
            elif new_key in self.account_state:
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
            await self.client.init(
                timeout=60, auto_close=False, close_delay=300, auto_refresh=True
            )
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
