"""Profile Vault & Auto Cookie Sync: quản lý N tài khoản Google không cần mở tab hay lưu mật khẩu.

Nguyên lý:
- Mỗi tài khoản Google có 1 thư mục profile trình duyệt riêng (tách biệt hoàn toàn với Chrome/Edge cá nhân).
- Người dùng chỉ tự đăng nhập tay + 2FA đúng 1 lần duy nhất trên trình duyệt thật (0 cờ automation).
- Mọi lần nạp lại cookie về sau chạy hoàn toàn ngầm (--headless=new) trong ~3-5 giây qua CDP cục bộ (127.0.0.1),
  lấy cặp __Secure-1PSID / __Secure-1PSIDTS tươi mới rồi đóng tiến trình trình duyệt ngay (0 tab, 0 MB RAM nền).
"""
from __future__ import annotations

import asyncio
import base64
from collections import deque
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
import uuid

from app.accounts import account_key
from app.config import (
    AUTO_COOKIE_SYNC,
    BASE_DIR,
    COOKIE_PROFILES_DIR,
    COOKIE_SYNC_BROWSER,
    COOKIE_SYNC_COOLDOWN,
    COOKIE_SYNC_TIMEOUT,
    build_instance_paths,
    logger,
)
from app.env_update import is_valid_cookie_value, is_valid_label, update_env_values

GEMINI_APP_URL = "https://gemini.google.com/app"
_PROFILE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_LANE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,16}$")
_LOGIN_URL_MARKERS = (
    "accounts.google.com/v3/signin",
    "accounts.google.com/servicelogin",
    "accounts.google.com/signin",
    "accounts.google.com/interactivelogin",
    "accounts.google.com/accountchooser",
    "accounts.google.com/o/oauth2",
)


def sanitize_profile_id(raw: str) -> str:
    val = (raw or "").strip()
    if not _PROFILE_ID_RE.fullmatch(val):
        raise ValueError(
            "profile_id chỉ được gồm chữ cái, số, '_' hoặc '-' (1..32 ký tự)."
        )
    return val


def sanitize_optional_lane_id(raw: Optional[str]) -> str:
    val = (raw or "").strip()
    if not val:
        return ""
    if val != "default" and not _LANE_ID_RE.fullmatch(val):
        raise ValueError(
            "lane_id không hợp lệ (chỉ gồm chữ cái, số, '_' hoặc '-', tối đa 16 ký tự)."
        )
    return val


def sanitize_optional_slot(raw: Any) -> Optional[int]:
    if raw is None or raw == "":
        return None
    try:
        slot = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("slot phải là số nguyên từ 0 đến 4.") from exc
    if not 0 <= slot <= 4:
        raise ValueError("slot phải nằm trong khoảng 0..4.")
    return slot


def find_browser_executable(preferred: str = "") -> Tuple[Optional[str], str]:
    """Tìm Chrome hoặc Edge đã cài sẵn trên máy (không cần tải Playwright/Chromium riêng)."""
    pref = (preferred or COOKIE_SYNC_BROWSER or "auto").strip()
    pref_low = pref.lower()

    if pref and pref_low not in ("auto", "chrome", "edge"):
        p = Path(pref)
        if p.is_file():
            name = "Edge" if "edge" in p.name.lower() else "Chrome"
            return str(p), name
        return None, ""

    local_app = os.environ.get("LOCALAPPDATA", "")
    chrome_candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        str(Path(local_app) / "Google" / "Chrome" / "Application" / "chrome.exe") if local_app else "",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        shutil.which("google-chrome") or "",
        shutil.which("chromium") or "",
        shutil.which("chromium-browser") or "",
        shutil.which("chrome") or "",
    ]
    edge_candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        str(Path(local_app) / "Microsoft" / "Edge" / "Application" / "msedge.exe") if local_app else "",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        shutil.which("msedge") or "",
        shutil.which("microsoft-edge") or "",
    ]

    if pref_low == "edge":
        groups = [("Edge", edge_candidates)]
    elif pref_low == "chrome":
        groups = [("Chrome", chrome_candidates)]
    else:
        groups = [("Chrome", chrome_candidates), ("Edge", edge_candidates)]

    for label, paths in groups:
        for candidate in paths:
            if candidate and Path(candidate).is_file():
                return str(Path(candidate)), label
    return None, ""


def _find_profile_browser_pids(profile_dir: Path) -> List[int]:
    """Tìm các PID Chrome/Edge đang mở ĐÚNG thư mục profile tách biệt này (tuyệt đối không đụng Chrome cá nhân)."""
    target = str(profile_dir.resolve()).lower()
    if ".cookie_profiles" not in target and not profile_dir.name:
        return []
    pids: List[int] = []
    try:
        if sys.platform == "win32":
            # Chỉ lọc tiến trình trình duyệt chính có --user-data-dir khớp chính xác thư mục profile
            ps_cmd = (
                "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe' OR Name='msedge.exe'\" "
                "| Where-Object { $_.CommandLine -and $_.CommandLine -notlike '*--type=*' "
                "  -and $_.CommandLine.ToLower().Contains($env:DSH_TARGET_PROFILE) } "
                "| ForEach-Object { $_.ProcessId }"
            )
            env = os.environ.copy()
            env["DSH_TARGET_PROFILE"] = target
            out = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                env=env,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=4.0,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            for line in out.splitlines():
                s = line.strip()
                if s.isdigit():
                    pids.append(int(s))
        else:
            out = subprocess.check_output(["ps", "-eo", "pid,args"], stderr=subprocess.DEVNULL, text=True, timeout=3.0)
            for line in out.splitlines():
                low = line.lower()
                if target in low and "--type=" not in low and ("chrome" in low or "edge" in low):
                    parts = line.strip().split(None, 1)
                    if parts and parts[0].isdigit():
                        pids.append(int(parts[0]))
    except Exception:
        return []
    return pids


def _terminate_pids_sync(pids: List[int]) -> None:
    if not pids:
        return
    for pid in pids:
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=4.0,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            else:
                os.kill(pid, 15)
        except Exception:
            pass


@dataclass
class CookieSyncResult:
    ok: bool
    profile_id: str
    psid: str = ""
    psidts: str = ""
    account_key: str = ""
    code: str = "ok"
    message: str = ""
    rotated: bool = False
    cookies: List[dict] = field(default_factory=list)


class CdpConnection:
    """Client WebSocket RFC 6455 tối giản (chỉ dùng stdlib) cho Chrome DevTools Protocol trên 127.0.0.1."""

    def __init__(self, host: str, port: int, path: str) -> None:
        self.host = host
        self.port = port
        self.path = path
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._next_id = 0
        self._events: deque[dict] = deque(maxlen=256)
        self._closed = False

    async def connect(self, timeout: float = 5.0) -> None:
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port),
            timeout=timeout,
        )
        ws_key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {ws_key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode("ascii")
        self._writer.write(req)
        await asyncio.wait_for(self._writer.drain(), timeout=timeout)
        raw_hdr = await asyncio.wait_for(
            self._reader.readuntil(b"\r\n\r\n"), timeout=timeout
        )
        status_line = raw_hdr.split(b"\r\n", 1)[0].decode("latin-1", errors="replace")
        if " 101 " not in status_line:
            await self.close()
            raise RuntimeError(f"CDP WebSocket handshake failed: {status_line}")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._writer is not None:
            try:
                await self._send_frame(0x08, b"")
            except Exception:
                pass
            try:
                self._writer.close()
                await asyncio.wait_for(self._writer.wait_closed(), timeout=1.0)
            except Exception:
                pass
        self._reader = None
        self._writer = None

    async def _send_frame(self, opcode: int, payload: bytes) -> None:
        if self._writer is None:
            raise RuntimeError("CDP connection is not open")
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
        n = len(payload)
        first = 0x80 | (opcode & 0x0F)
        if n < 126:
            header = struct.pack("!BB", first, 0x80 | n)
        elif n < 65536:
            header = struct.pack("!BBH", first, 0x80 | 126, n)
        else:
            header = struct.pack("!BBQ", first, 0x80 | 127, n)
        self._writer.write(header + mask + masked)
        await self._writer.drain()

    async def _read_message(self, timeout: float) -> Optional[dict]:
        if self._reader is None:
            return None
        deadline = time.monotonic() + max(0.05, timeout)
        chunks: List[bytes] = []
        msg_opcode: Optional[int] = None

        while True:
            rem = deadline - time.monotonic()
            if rem <= 0:
                raise asyncio.TimeoutError("Timed out reading CDP frame")
            hdr = await asyncio.wait_for(self._reader.readexactly(2), timeout=rem)
            fin = bool(hdr[0] & 0x80)
            opcode = hdr[0] & 0x0F
            masked = bool(hdr[1] & 0x80)
            ln = hdr[1] & 0x7F
            if ln == 126:
                rem = max(0.05, deadline - time.monotonic())
                ln = struct.unpack(
                    "!H",
                    await asyncio.wait_for(self._reader.readexactly(2), timeout=rem),
                )[0]
            elif ln == 127:
                rem = max(0.05, deadline - time.monotonic())
                ln = struct.unpack(
                    "!Q",
                    await asyncio.wait_for(self._reader.readexactly(8), timeout=rem),
                )[0]
            mask_key = b""
            if masked:
                rem = max(0.05, deadline - time.monotonic())
                mask_key = await asyncio.wait_for(
                    self._reader.readexactly(4), timeout=rem
                )
            rem = max(0.05, deadline - time.monotonic())
            payload = (
                await asyncio.wait_for(self._reader.readexactly(ln), timeout=rem)
                if ln
                else b""
            )
            if masked and payload:
                payload = bytes(b ^ mask_key[i & 3] for i, b in enumerate(payload))

            if opcode == 0x08:  # Close
                return None
            if opcode == 0x09:  # Ping -> Pong
                try:
                    await self._send_frame(0x0A, payload)
                except Exception:
                    pass
                continue
            if opcode == 0x0A:  # Pong
                continue

            if opcode != 0x00:
                msg_opcode = opcode
                chunks = [payload]
            else:
                chunks.append(payload)

            if fin:
                if msg_opcode == 0x01:
                    raw = b"".join(chunks).decode("utf-8", errors="replace")
                    return json.loads(raw)
                chunks = []
                msg_opcode = None

    async def call(
        self, method: str, params: Optional[dict] = None, timeout: float = 8.0
    ) -> dict:
        self._next_id += 1
        req_id = self._next_id
        payload = json.dumps(
            {"id": req_id, "method": method, "params": params or {}},
            ensure_ascii=False,
        ).encode("utf-8")
        await self._send_frame(0x01, payload)

        deadline = time.monotonic() + max(0.2, timeout)
        while True:
            rem = deadline - time.monotonic()
            if rem <= 0:
                raise asyncio.TimeoutError(f"CDP call '{method}' timed out")
            msg = await self._read_message(rem)
            if msg is None:
                raise RuntimeError(f"CDP connection closed while waiting for '{method}'")
            if msg.get("id") == req_id:
                if "error" in msg:
                    err = msg["error"]
                    raise RuntimeError(
                        f"CDP '{method}' error: {err.get('message', err)}"
                    )
                return msg.get("result") or {}
            if "method" in msg:
                self._events.append(msg)

    async def wait_for_event(self, event_name: str, timeout: float = 10.0) -> Optional[dict]:
        for idx, ev in enumerate(self._events):
            if ev.get("method") == event_name:
                del self._events[idx]
                return ev.get("params") or {}
        deadline = time.monotonic() + max(0.1, timeout)
        while True:
            rem = deadline - time.monotonic()
            if rem <= 0:
                return None
            try:
                msg = await self._read_message(rem)
            except asyncio.TimeoutError:
                return None
            if msg is None:
                return None
            if msg.get("method") == event_name:
                return msg.get("params") or {}
            if "method" in msg:
                self._events.append(msg)


def extract_gemini_cookies(cookies: List[dict]) -> Tuple[str, str]:
    """Lọc ra cặp (__Secure-1PSID, __Secure-1PSIDTS) tốt nhất từ danh sách cookie CDP."""
    now = time.time()
    best: Dict[str, Tuple[int, float, str]] = {}

    for c in cookies or []:
        name = str(c.get("name") or "")
        if name not in ("__Secure-1PSID", "__Secure-1PSIDTS"):
            continue
        value = str(c.get("value") or "").strip()
        if not value or not is_valid_cookie_value(value):
            continue
        domain = str(c.get("domain") or "").lower().strip()
        if domain not in (".google.com", "google.com", "gemini.google.com", ".gemini.google.com"):
            continue
        expires = float(c.get("expires") or -1)
        if expires > 0 and expires < now:
            continue
        # Ưu tiên domain .google.com (chuẩn dùng cho gemini_webapi) rồi tới hạn dài hơn
        prio = 2 if domain == ".google.com" else (1 if domain == "google.com" else 0)
        cand = (prio, expires, value)
        prev = best.get(name)
        if prev is None or cand[:2] > prev[:2]:
            best[name] = cand

    psid = best["__Secure-1PSID"][2] if "__Secure-1PSID" in best else ""
    psidts = best["__Secure-1PSIDTS"][2] if "__Secure-1PSIDTS" in best else ""
    return psid, psidts


_GEMINI_JAR_DOMAINS = {".google.com", "google.com", "gemini.google.com", ".gemini.google.com"}
_SKIP_ANALYTICS_PREFIXES = ("_ga", "_gcl_", "_gid")


def extract_gemini_cookie_jar(cookies: List[dict]) -> List[dict]:
    """Lọc bộ cookie .google.com + gemini.google.com cần thiết (gồm OSID/SID/1PSID/1PSIDTS/1PSIDCC/NID).

    Cố ý loại trừ cookie nhạy cảm riêng của accounts.google.com (LSID, __Host-GAPS) và cookie analytics.
    """
    now = time.time()
    out: List[dict] = []
    seen: set[Tuple[str, str, str]] = set()
    for c in cookies or []:
        name = str(c.get("name") or "").strip()
        value = str(c.get("value") or "").strip()
        domain = str(c.get("domain") or "").strip().lower()
        path = str(c.get("path") or "/").strip() or "/"
        if not name or not value or domain not in _GEMINI_JAR_DOMAINS:
            continue
        if any(name.startswith(pfx) for pfx in _SKIP_ANALYTICS_PREFIXES):
            continue
        raw_exp = c.get("expires")
        try:
            exp_val = float(raw_exp) if raw_exp is not None else -1.0
        except (TypeError, ValueError):
            exp_val = -1.0
        if exp_val > 0 and exp_val < now:
            continue
        key = (name, domain, path)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "name": name,
                "value": value,
                "domain": domain,
                "path": path,
                "expires": exp_val if exp_val > 0 else None,
            }
        )
    return out


def persist_cookie_jar_to_lane_cache(
    base_dir: Path,
    lane_id: str,
    psid: str,
    cookies: Optional[List[dict]],
) -> Optional[Path]:
    """Ghi nguyên tử bộ cookie Google/Gemini vào .gemini_cookies_<lane>/.cached_cookies_<PSID>.json
    để gemini-webapi nạp đủ OSID/SID/1PSIDCC ngay khi khởi tạo (tránh redirect ServiceLogin?osid=1)."""
    if not psid or not cookies:
        return None
    inst_id = "" if lane_id in ("", "default", "self") else lane_id
    cookie_dir = Path(build_instance_paths(base_dir, inst_id)["cookie_dir"])
    safe_jar = extract_gemini_cookie_jar(cookies)
    if not safe_jar:
        return None
    try:
        cookie_dir.mkdir(parents=True, exist_ok=True)
        target = cookie_dir / f".cached_cookies_{psid}.json"
        tmp = cookie_dir / f".tmp_jar_{uuid.uuid4().hex}.json"
        tmp.write_text(json.dumps(safe_jar, ensure_ascii=False), encoding="utf-8")
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        os.replace(tmp, target)
        return target
    except Exception as exc:
        logger.warning("[COOKIE-SYNC] Không ghi được cookie jar cache cho '%s': %s", lane_id or "default", exc)
        return None


def _is_login_redirect_url(url: str) -> bool:
    low = (url or "").strip().lower()
    if not low:
        return False
    return any(marker in low for marker in _LOGIN_URL_MARKERS)


def persist_cookies_to_lane_env(
    base_dir: Path,
    lane_id: str,
    slot: int,
    psid: str,
    psidts: str,
    label: str = "",
) -> Tuple[bool, str, Optional[str]]:
    """Ghi nguyên tử cookie vào .env hoặc .env.<lane_id> (dùng được cả khi worker đang tắt)."""
    inst_id = "" if lane_id in ("", "default") else lane_id
    env_file = Path(build_instance_paths(base_dir, inst_id)["env_file"])
    if slot == 0:
        updates = {"SECURE_1PSID": psid, "SECURE_1PSIDTS": psidts}
    else:
        updates = {f"BACKUP_{slot}PSID": psid, f"BACKUP_{slot}PSIDTS": psidts}
        if label and is_valid_label(label):
            updates[f"BACKUP_{slot}_LABEL"] = label
    try:
        content = env_file.read_text(encoding="utf-8") if env_file.exists() else ""
        new_content = update_env_values(content, updates)
        tmp_file = env_file.parent / f".tmp_{env_file.name}_{uuid.uuid4().hex}"
        tmp_file.write_text(new_content, encoding="utf-8")
        os.replace(tmp_file, env_file)
        return True, env_file.name, None
    except Exception as exc:
        logger.error("[COOKIE-SYNC] Không ghi được %s: %s", env_file.name, exc)
        return False, env_file.name, str(exc)


class ProfileVault:
    """Kho lưu metadata N tài khoản Google (TUYỆT ĐỐI không lưu raw cookie hay mật khẩu trong vault.json)."""

    def __init__(self, profiles_dir: Optional[Path] = None) -> None:
        self.profiles_dir = Path(profiles_dir or COOKIE_PROFILES_DIR)
        self.vault_file = self.profiles_dir / "vault.json"

    def profile_dir(self, profile_id: str) -> Path:
        pid = sanitize_profile_id(profile_id)
        return self.profiles_dir / pid

    def has_profile_data(self, profile_id: str) -> bool:
        pdir = self.profile_dir(profile_id)
        if not pdir.is_dir():
            return False
        markers = (
            pdir / "Default" / "Network" / "Cookies",
            pdir / "Default" / "Cookies",
            pdir / "Local State",
        )
        return any(m.is_file() for m in markers)

    def _load_raw(self) -> Dict[str, dict]:
        if not self.vault_file.is_file():
            return {}
        try:
            data = json.loads(self.vault_file.read_text(encoding="utf-8"))
            profiles = data.get("profiles") if isinstance(data, dict) else {}
            if not isinstance(profiles, dict):
                return {}
            return {
                k: v for k, v in profiles.items() if isinstance(k, str) and isinstance(v, dict)
            }
        except Exception as exc:
            logger.warning("[COOKIE-SYNC] Không đọc được vault.json: %s", exc)
            return {}

    def _save_raw(self, profiles: Dict[str, dict]) -> None:
        self.profiles_dir.mkdir(parents=True, exist_ok=True)
        # Chỉ cho phép lưu các trường metadata an toàn, chặn tuyệt đối psid/psidts/password
        safe_profiles: Dict[str, dict] = {}
        allowed_keys = {
            "profile_id",
            "label",
            "lane_id",
            "slot",
            "created_at",
            "last_synced_at",
            "last_status",
            "last_error",
            "account_key",
        }
        for pid, meta in profiles.items():
            safe_profiles[pid] = {k: v for k, v in meta.items() if k in allowed_keys}
        payload = json.dumps({"version": 1, "profiles": safe_profiles}, ensure_ascii=False, indent=2)
        tmp = self.profiles_dir / f".tmp_vault_{uuid.uuid4().hex}.json"
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, self.vault_file)

    def get_profile(self, profile_id: str) -> Optional[dict]:
        pid = sanitize_profile_id(profile_id)
        data = self._load_raw().get(pid)
        if not data:
            return None
        return self._enrich(pid, data)

    def _enrich(self, pid: str, meta: dict) -> dict:
        out = dict(meta)
        out["profile_id"] = pid
        out.setdefault("label", pid)
        out.setdefault("lane_id", "")
        out.setdefault("slot", None)
        out.setdefault("last_synced_at", 0)
        out.setdefault("last_status", "new")
        out.setdefault("last_error", "")
        out.setdefault("account_key", "")
        out["has_profile_data"] = self.has_profile_data(pid)
        return out

    def list_profiles(self) -> List[dict]:
        raw = self._load_raw()
        items = [self._enrich(pid, meta) for pid, meta in raw.items()]
        items.sort(key=lambda x: (x.get("lane_id") or "zzz", x.get("slot") if x.get("slot") is not None else 99, x["profile_id"]))
        return items

    def upsert_profile(
        self,
        profile_id: str,
        *,
        label: Optional[str] = None,
        lane_id: Optional[str] = None,
        slot: Any = None,
        clear_binding: bool = False,
    ) -> dict:
        pid = sanitize_profile_id(profile_id)
        clean_label = (label or "").strip()
        if clean_label and not is_valid_label(clean_label):
            raise ValueError("Nhãn profile chứa ký tự không hợp lệ hoặc quá 40 ký tự.")
        clean_lane = sanitize_optional_lane_id(lane_id) if lane_id is not None else None
        clean_slot = sanitize_optional_slot(slot) if slot is not None else None

        profiles = self._load_raw()
        existing = dict(profiles.get(pid) or {})
        now = int(time.time())
        existing.setdefault("profile_id", pid)
        existing.setdefault("created_at", now)
        existing.setdefault("last_synced_at", 0)
        existing.setdefault("last_status", "new")
        existing.setdefault("last_error", "")
        existing.setdefault("account_key", "")

        if clean_label:
            existing["label"] = clean_label
        elif "label" not in existing:
            existing["label"] = pid

        if clear_binding:
            existing["lane_id"] = ""
            existing["slot"] = None
        else:
            if clean_lane is not None:
                existing["lane_id"] = clean_lane
            if clean_slot is not None:
                existing["slot"] = clean_slot
            elif existing.get("lane_id") and existing.get("slot") is None:
                existing["slot"] = 0

        # Nếu gán (lane_id, slot) trùng với profile khác -> gỡ gán ở profile cũ để tránh xung đột 2 profile cùng 1 ô
        bound_lane = existing.get("lane_id") or ""
        bound_slot = existing.get("slot")
        if bound_lane and bound_slot is not None:
            for other_pid, other_meta in profiles.items():
                if (
                    other_pid != pid
                    and (other_meta.get("lane_id") or "") == bound_lane
                    and other_meta.get("slot") == bound_slot
                ):
                    other_meta["lane_id"] = ""
                    other_meta["slot"] = None

        profiles[pid] = existing
        self._save_raw(profiles)
        return self._enrich(pid, existing)

    def record_sync_status(
        self,
        profile_id: str,
        *,
        status: str,
        error: str = "",
        acc_key: str = "",
        synced_at: Optional[int] = None,
    ) -> dict:
        pid = sanitize_profile_id(profile_id)
        profiles = self._load_raw()
        existing = dict(profiles.get(pid) or {"profile_id": pid, "label": pid, "created_at": int(time.time())})
        existing["last_status"] = status
        existing["last_error"] = (error or "")[:240]
        if acc_key:
            existing["account_key"] = acc_key
        if synced_at is not None:
            existing["last_synced_at"] = int(synced_at)
        profiles[pid] = existing
        self._save_raw(profiles)
        return self._enrich(pid, existing)

    def delete_profile(self, profile_id: str, remove_dir: bool = True) -> bool:
        pid = sanitize_profile_id(profile_id)
        profiles = self._load_raw()
        existed = pid in profiles
        if existed:
            del profiles[pid]
            self._save_raw(profiles)
        pdir = self.profile_dir(pid)
        if remove_dir and pdir.exists():
            shutil.rmtree(pdir, ignore_errors=True)
            existed = True
        return existed

    def find_profile_for_slot(self, lane_id: str, slot: int = 0) -> Optional[dict]:
        """Tìm profile đã đăng nhập gắn với (lane_id, slot) để tự động phục hồi khi hết hạn."""
        target_lane = (lane_id or "default").strip() or "default"
        raw = self._load_raw()
        # 1. Khớp chính xác (lane_id, slot)
        for pid, meta in raw.items():
            m_lane = (meta.get("lane_id") or "").strip()
            m_slot = meta.get("slot")
            if m_lane == target_lane and m_slot == slot and self.has_profile_data(pid):
                return self._enrich(pid, meta)
        # 2. Fallback tiện lợi cho slot 0: profile có tên trùng đúng lane_id (vd profile 'acc1')
        if slot == 0 and target_lane in raw:
            meta = raw[target_lane]
            m_lane = (meta.get("lane_id") or "").strip()
            if m_lane in ("", target_lane) and self.has_profile_data(target_lane):
                return self._enrich(target_lane, meta)
        return None


class CookieSyncManager:
    """Điều phối chạy trình duyệt tuần tự (1 tiến trình/lần), đăng nhập 1 lần và sync ngầm."""

    def __init__(
        self,
        vault: Optional[ProfileVault] = None,
        browser_preference: str = "",
        sync_timeout: Optional[float] = None,
        cooldown_seconds: Optional[float] = None,
    ) -> None:
        self.vault = vault or ProfileVault()
        self.browser_preference = browser_preference or COOKIE_SYNC_BROWSER
        self.sync_timeout = float(sync_timeout or COOKIE_SYNC_TIMEOUT)
        self.cooldown_seconds = float(cooldown_seconds or COOKIE_SYNC_COOLDOWN)
        self._browser_sem = asyncio.Semaphore(1)
        self._profile_locks: Dict[str, asyncio.Lock] = {}
        self._active_logins: Dict[str, subprocess.Popen] = {}
        self._active_login_tasks: Dict[str, asyncio.Task] = {}
        self._active_syncs: set[str] = set()
        self._last_auto_sync_ts: Dict[str, float] = {}

    def _lock_for(self, profile_id: str) -> asyncio.Lock:
        lock = self._profile_locks.get(profile_id)
        if lock is None:
            lock = asyncio.Lock()
            self._profile_locks[profile_id] = lock
        return lock

    def is_login_open(self, profile_id: str) -> bool:
        proc = self._active_logins.get(profile_id)
        if proc is None:
            return False
        if proc.poll() is not None:
            self._active_logins.pop(profile_id, None)
            return False
        return True

    def browser_info(self) -> dict:
        exe, name = find_browser_executable(self.browser_preference)
        return {
            "browser_available": bool(exe),
            "browser_name": name or "",
            "auto_sync_enabled": bool(AUTO_COOKIE_SYNC),
        }

    def list_profiles_with_runtime(self) -> List[dict]:
        items = self.vault.list_profiles()
        for item in items:
            pid = item["profile_id"]
            if self.is_login_open(pid):
                item["runtime_state"] = "waiting_login"
            elif pid in self._active_syncs:
                item["runtime_state"] = "syncing"
            else:
                item["runtime_state"] = "idle"
        return items

    def can_auto_sync_now(self, profile_id: str) -> bool:
        last = self._last_auto_sync_ts.get(profile_id, 0.0)
        return (time.time() - last) >= self.cooldown_seconds

    def mark_auto_sync_attempt(self, profile_id: str) -> None:
        self._last_auto_sync_ts[profile_id] = time.time()

    async def sync_profile(
        self,
        profile_id: str,
        *,
        timeout: Optional[float] = None,
    ) -> CookieSyncResult:
        """Mở trình duyệt ẩn trong ~3-5 giây với đúng profile đã đăng nhập, lấy cặp 1PSID/1PSIDTS mới rồi đóng ngay."""
        pid = sanitize_profile_id(profile_id)
        if self.is_login_open(pid):
            return CookieSyncResult(
                ok=False,
                profile_id=pid,
                code="login_window_open",
                message="Cửa sổ đăng nhập của profile này đang mở. Hãy đăng nhập xong và đóng cửa sổ trước.",
            )
        if not self.vault.has_profile_data(pid):
            self.vault.record_sync_status(
                pid,
                status="login_required",
                error="Chưa đăng nhập lần đầu cho profile này.",
            )
            return CookieSyncResult(
                ok=False,
                profile_id=pid,
                code="profile_login_required",
                message=f"Profile '{pid}' chưa từng đăng nhập. Hãy bấm 'Đăng nhập 1 lần' trước.",
            )

        exe, browser_name = find_browser_executable(self.browser_preference)
        if not exe:
            return CookieSyncResult(
                ok=False,
                profile_id=pid,
                code="browser_not_found",
                message="Không tìm thấy Chrome hoặc Microsoft Edge trên máy (cấu hình COOKIE_SYNC_BROWSER nếu cài ở đường dẫn khác).",
            )

        effective_timeout = float(timeout or self.sync_timeout)
        p_lock = self._lock_for(pid)

        async with p_lock:
            async with self._browser_sem:
                self._active_syncs.add(pid)
                try:
                    result = await self._run_headless_extract(
                        pid, exe, browser_name, effective_timeout
                    )
                finally:
                    self._active_syncs.discard(pid)

        if result.ok:
            self.vault.record_sync_status(
                pid,
                status="synced",
                error="",
                acc_key=result.account_key,
                synced_at=int(time.time()),
            )
        else:
            if result.code == "browser_exited_early":
                orphan_pids = await asyncio.to_thread(
                    _find_profile_browser_pids, self.vault.profile_dir(pid)
                )
                if orphan_pids:
                    result = CookieSyncResult(
                        ok=False,
                        profile_id=pid,
                        code="login_window_open",
                        message=f"Cửa sổ Chrome của profile '{pid}' vẫn đang mở (PID {orphan_pids[0]}). Hãy đóng cửa sổ đó hoặc bấm nút '✅ Xong & Sync'.",
                    )
            if result.code == "login_window_open":
                st = "waiting_login"
            elif result.code == "profile_login_required":
                st = "login_required"
            else:
                st = "error"
            self.vault.record_sync_status(pid, status=st, error=result.message)
        return result

    async def _run_headless_extract(
        self,
        profile_id: str,
        browser_exe: str,
        browser_name: str,
        timeout: float,
    ) -> CookieSyncResult:
        pdir = self.vault.profile_dir(profile_id)
        pdir.mkdir(parents=True, exist_ok=True)
        port_file = pdir / "DevToolsActivePort"
        try:
            if port_file.exists():
                port_file.unlink()
        except OSError:
            pass

        cmd = [
            browser_exe,
            "--headless=new",
            f"--user-data-dir={pdir}",
            "--remote-debugging-port=0",
            "--remote-debugging-address=127.0.0.1",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-mode",
            "--disable-gpu",
            "--disable-extensions",
            "--mute-audio",
            "--disk-cache-size=1048576",
            "about:blank",
        ]
        popen_kw: Dict[str, Any] = {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if sys.platform == "win32":
            popen_kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        proc: Optional[subprocess.Popen] = None
        browser_cdp: Optional[CdpConnection] = None
        page_cdp: Optional[CdpConnection] = None
        deadline = time.monotonic() + max(10.0, timeout)

        logger.info(
            "[COOKIE-SYNC] Bắt đầu sync ngầm profile '%s' bằng %s (headless)...",
            profile_id,
            browser_name,
        )
        try:
            proc = subprocess.Popen(cmd, **popen_kw)

            # 1. Chờ DevToolsActivePort (cổng cục bộ ngẫu nhiên trên 127.0.0.1)
            port = 0
            ws_path = ""
            port_deadline = min(deadline, time.monotonic() + 10.0)
            while time.monotonic() < port_deadline:
                if proc.poll() is not None:
                    return CookieSyncResult(
                        ok=False,
                        profile_id=profile_id,
                        code="browser_exited_early",
                        message=f"{browser_name} thoát sớm khi mở profile '{profile_id}' (có thể profile đang bị tiến trình khác khóa).",
                    )
                if port_file.is_file():
                    try:
                        lines = port_file.read_text(encoding="utf-8").splitlines()
                        if len(lines) >= 2 and lines[0].strip().isdigit():
                            port = int(lines[0].strip())
                            ws_path = lines[1].strip()
                            break
                    except OSError:
                        pass
                await asyncio.sleep(0.1)

            if not port or not ws_path:
                return CookieSyncResult(
                    ok=False,
                    profile_id=profile_id,
                    code="cdp_startup_timeout",
                    message=f"Quá thời gian chờ {browser_name} khởi động CDP cục bộ.",
                )

            # 2. Kết nối browser CDP và chụp giá trị 1PSIDTS cũ (nếu có) trước khi vào Gemini
            browser_cdp = CdpConnection("127.0.0.1", port, ws_path)
            await browser_cdp.connect(timeout=min(5.0, max(1.0, deadline - time.monotonic())))
            pre_res = await browser_cdp.call("Storage.getCookies", timeout=5.0)
            _, initial_psidts = extract_gemini_cookies(pre_res.get("cookies") or [])

            # 3. Mở tab ngầm tới gemini.google.com/app để Google tự quay vòng cookie theo phiên đã đăng nhập
            target_res = await browser_cdp.call(
                "Target.createTarget",
                {"url": GEMINI_APP_URL},
                timeout=5.0,
            )
            target_id = str(target_res.get("targetId") or "").strip()
            if not target_id:
                return CookieSyncResult(
                    ok=False,
                    profile_id=profile_id,
                    code="cdp_target_failed",
                    message="Không tạo được target trình duyệt ngầm.",
                )

            page_cdp = CdpConnection("127.0.0.1", port, f"/devtools/page/{target_id}")
            await page_cdp.connect(timeout=min(5.0, max(1.0, deadline - time.monotonic())))
            await page_cdp.call("Page.enable", timeout=5.0)

            # Chờ trang tải xong (Page.loadEventFired hoặc document.readyState == complete)
            load_wait = min(12.0, max(2.0, deadline - time.monotonic() - 4.0))
            loaded = await page_cdp.wait_for_event("Page.loadEventFired", timeout=load_wait)
            if loaded is None:
                # Kiểm tra thêm readyState trong trường hợp sự kiện đã bắn trước khi Page.enable
                try:
                    rs = await page_cdp.call(
                        "Runtime.evaluate",
                        {"expression": "document.readyState", "returnByValue": True},
                        timeout=3.0,
                    )
                    _ = (rs.get("result") or {}).get("value")
                except Exception:
                    pass

            # 4. Kiểm tra xem có bị chuyển hướng về trang đăng nhập Google không
            current_url = ""
            try:
                loc = await page_cdp.call(
                    "Runtime.evaluate",
                    {"expression": "window.location.href", "returnByValue": True},
                    timeout=3.0,
                )
                current_url = str((loc.get("result") or {}).get("value") or "")
            except Exception:
                current_url = ""

            if _is_login_redirect_url(current_url):
                return CookieSyncResult(
                    ok=False,
                    profile_id=profile_id,
                    code="profile_login_required",
                    message=f"Phiên Google trong profile '{profile_id}' đã hết hạn hoặc chưa đăng nhập. Hãy bấm 'Đăng nhập 1 lần'.",
                )

            # 5. Chờ ngắn để Google hoàn tất RotateCookies và đọc cặp cookie mới nhất
            psid, psidts = "", ""
            latest_raw_cookies: List[dict] = []
            rotated = False
            settle_start = time.monotonic()
            poll_deadline = min(deadline, settle_start + 6.0)
            while time.monotonic() < poll_deadline:
                cookie_res = await browser_cdp.call("Storage.getCookies", timeout=5.0)
                latest_raw_cookies = cookie_res.get("cookies") or []
                psid, psidts = extract_gemini_cookies(latest_raw_cookies)
                if psid and psidts:
                    if initial_psidts and psidts != initial_psidts:
                        rotated = True
                        break
                    if (time.monotonic() - settle_start) >= 2.2:
                        break
                await asyncio.sleep(0.5)

            if not psid or not psidts:
                return CookieSyncResult(
                    ok=False,
                    profile_id=profile_id,
                    code="profile_login_required",
                    message=f"Không tìm thấy cặp __Secure-1PSID/__Secure-1PSIDTS hợp lệ trong profile '{profile_id}'. Hãy đăng nhập lại 1 lần.",
                )

            cookie_jar = extract_gemini_cookie_jar(latest_raw_cookies)
            key = account_key(psid)
            logger.info(
                "[COOKIE-SYNC] Sync ngầm profile '%s' thành công (account=%s, rotated=%s, jar=%d cookies).",
                profile_id,
                key,
                rotated,
                len(cookie_jar),
            )
            return CookieSyncResult(
                ok=True,
                profile_id=profile_id,
                psid=psid,
                psidts=psidts,
                account_key=key,
                code="ok",
                message="Đã đồng bộ cookie mới từ profile.",
                rotated=rotated,
                cookies=cookie_jar,
            )
        except asyncio.TimeoutError:
            return CookieSyncResult(
                ok=False,
                profile_id=profile_id,
                code="sync_timeout",
                message=f"Hết thời gian đồng bộ ngầm profile '{profile_id}' ({int(timeout)}s).",
            )
        except Exception as exc:
            logger.error("[COOKIE-SYNC] Lỗi sync profile '%s': %s", profile_id, exc)
            return CookieSyncResult(
                ok=False,
                profile_id=profile_id,
                code="sync_failed",
                message=f"Lỗi đồng bộ profile '{profile_id}': {exc}",
            )
        finally:
            if page_cdp is not None:
                await page_cdp.close()
            if browser_cdp is not None:
                try:
                    # Đóng sạch qua CDP để Chrome/Edge flush SQLite Cookies xuống đĩa trước khi thoát
                    await browser_cdp.call("Browser.close", timeout=2.0)
                except Exception:
                    pass
                await browser_cdp.close()
            if proc is not None and proc.poll() is None:
                try:
                    await asyncio.wait_for(asyncio.to_thread(proc.wait), timeout=4.0)
                except Exception:
                    try:
                        proc.terminate()
                        await asyncio.wait_for(asyncio.to_thread(proc.wait), timeout=2.0)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass

    async def start_interactive_login(
        self,
        profile_id: str,
        *,
        label: Optional[str] = None,
        lane_id: Optional[str] = None,
        slot: Any = None,
        on_complete: Optional[Callable[[CookieSyncResult, dict], Awaitable[None]]] = None,
    ) -> Tuple[dict, subprocess.Popen]:
        """Mở cửa sổ trình duyệt thật (KHÔNG cờ automation) để người dùng tự đăng nhập + 2FA đúng 1 lần."""
        pid = sanitize_profile_id(profile_id)
        if self.is_login_open(pid):
            raise RuntimeError(f"Cửa sổ đăng nhập của profile '{pid}' đang mở rồi.")
        if pid in self._active_syncs:
            raise RuntimeError(f"Profile '{pid}' đang sync ngầm, thử lại sau vài giây.")

        exe, browser_name = find_browser_executable(self.browser_preference)
        if not exe:
            raise RuntimeError(
                "Không tìm thấy Chrome hoặc Microsoft Edge trên máy (cấu hình COOKIE_SYNC_BROWSER nếu cần)."
            )

        meta = self.vault.upsert_profile(pid, label=label, lane_id=lane_id, slot=slot)
        pdir = self.vault.profile_dir(pid)
        pdir.mkdir(parents=True, exist_ok=True)

        existing_pids = await asyncio.to_thread(_find_profile_browser_pids, pdir)
        if existing_pids:
            self.vault.record_sync_status(
                pid,
                status="waiting_login",
                error=f"Cửa sổ {browser_name} của profile '{pid}' đang mở sẵn (PID {existing_pids[0]}). Đăng nhập xong bấm '✅ Xong & Sync'.",
            )
            raise RuntimeError(
                f"Cửa sổ {browser_name} của profile '{pid}' đang mở sẵn rồi (PID {existing_pids[0]}). Hãy đăng nhập xong rồi bấm nút '✅ Xong & Sync'."
            )

        # Cố ý KHÔNG truyền --remote-debugging-port hay --enable-automation để Google nhận 100% trình duyệt thật
        cmd = [
            exe,
            f"--user-data-dir={pdir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-mode",
            GEMINI_APP_URL,
        ]
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._active_logins[pid] = proc
        self.vault.record_sync_status(
            pid,
            status="waiting_login",
            error=f"Đang mở {browser_name} để đăng nhập 1 lần — đăng nhập xong hãy đóng cửa sổ trình duyệt.",
        )
        logger.info(
            "[COOKIE-SYNC] Đã mở cửa sổ %s thật cho profile '%s' (chờ người dùng đăng nhập và đóng cửa sổ).",
            browser_name,
            pid,
        )

        async def _watch_and_sync() -> None:
            try:
                await asyncio.to_thread(proc.wait)
            finally:
                self._active_logins.pop(pid, None)
            # Chờ nhẹ để OS giải phóng khóa file SQLite sau khi cửa sổ đóng
            await asyncio.sleep(0.8)
            latest_meta = self.vault.get_profile(pid) or meta
            res = await self.sync_profile(pid)
            if on_complete is not None:
                try:
                    await on_complete(res, latest_meta)
                except Exception as exc:
                    logger.error(
                        "[COOKIE-SYNC] Lỗi đẩy cookie sau khi đăng nhập profile '%s': %s",
                        pid,
                        exc,
                    )

        task = asyncio.create_task(_watch_and_sync())
        self._active_login_tasks[pid] = task
        task.add_done_callback(lambda _t: self._active_login_tasks.pop(pid, None))
        return meta, proc

    async def finish_interactive_login(self, profile_id: str) -> CookieSyncResult:
        """Đóng cửa sổ Chrome đăng nhập đang mở (nếu còn) rồi chạy sync ngầm ngay lập tức."""
        pid = sanitize_profile_id(profile_id)
        watch_task = self._active_login_tasks.get(pid)
        if watch_task is not None and not watch_task.done():
            watch_task.cancel()
            try:
                await watch_task
            except (asyncio.CancelledError, Exception):
                pass
        proc = self._active_logins.pop(pid, None)
        closed_any = False
        if proc is not None and proc.poll() is None:
            closed_any = True
            try:
                proc.terminate()
                await asyncio.wait_for(asyncio.to_thread(proc.wait), timeout=3.5)
            except Exception:
                try:
                    proc.kill()
                    await asyncio.wait_for(asyncio.to_thread(proc.wait), timeout=2.0)
                except Exception:
                    pass
        orphan_pids = await asyncio.to_thread(
            _find_profile_browser_pids, self.vault.profile_dir(pid)
        )
        if orphan_pids:
            closed_any = True
            await asyncio.to_thread(_terminate_pids_sync, orphan_pids)
        if closed_any:
            await asyncio.sleep(0.9)
        return await self.sync_profile(pid)


cookie_sync_manager = CookieSyncManager()


async def try_auto_sync_for_worker_slot(
    lane_id: str, slot: int = 0
) -> Optional[CookieSyncResult]:
    """Tự động gọi sync ngầm nếu có profile trong kho gắn với (lane_id, slot) và hết cooldown."""
    if not AUTO_COOKIE_SYNC:
        return None
    mgr = cookie_sync_manager
    profile = mgr.vault.find_profile_for_slot(lane_id, slot)
    if not profile:
        return None
    pid = profile["profile_id"]
    if mgr.is_login_open(pid) or not mgr.can_auto_sync_now(pid):
        return None
    mgr.mark_auto_sync_attempt(pid)
    logger.warning(
        "[COOKIE-SYNC] Phát hiện luồng '%s' (slot %s) mất xác thực -> thử tự nạp ngầm từ profile '%s'...",
        lane_id or "default",
        slot,
        pid,
    )
    return await mgr.sync_profile(pid)


__all__ = [
    "BASE_DIR",
    "CdpConnection",
    "CookieSyncManager",
    "CookieSyncResult",
    "GEMINI_APP_URL",
    "ProfileVault",
    "cookie_sync_manager",
    "extract_gemini_cookie_jar",
    "extract_gemini_cookies",
    "find_browser_executable",
    "persist_cookie_jar_to_lane_cache",
    "persist_cookies_to_lane_env",
    "sanitize_optional_lane_id",
    "sanitize_optional_slot",
    "sanitize_profile_id",
    "try_auto_sync_for_worker_slot",
]
