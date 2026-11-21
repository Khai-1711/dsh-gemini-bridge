"""Tests cho Profile Vault, CDP WebSocket stdlib client, Auto Cookie Sync và Admin endpoints."""
import asyncio
import base64
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
from gemini_webapi.constants import AccountStatus

from app.account_pool import _safe_init_client
from app.cookie_sync import (
    CdpConnection,
    CookieSyncManager,
    CookieSyncResult,
    ProfileVault,
    extract_gemini_cookie_jar,
    extract_gemini_cookies,
    persist_cookie_jar_to_lane_cache,
    persist_cookies_to_lane_env,
    sanitize_optional_lane_id,
    sanitize_optional_slot,
    sanitize_profile_id,
)
from gemini_webapi.exceptions import AuthError
from app.gemini_session import SingleChatManager
import main as app_main


def _encode_server_ws_frame(opcode: int, payload: bytes, *, fin: bool = True) -> bytes:
    first = (0x80 if fin else 0x00) | (opcode & 0x0F)
    n = len(payload)
    if n < 126:
        hdr = struct.pack("!BB", first, n)
    elif n < 65536:
        hdr = struct.pack("!BBH", first, 126, n)
    else:
        hdr = struct.pack("!BBQ", first, 127, n)
    return hdr + payload


async def _read_client_ws_frame(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    hdr = await reader.readexactly(2)
    opcode = hdr[0] & 0x0F
    masked = bool(hdr[1] & 0x80)
    ln = hdr[1] & 0x7F
    if ln == 126:
        ln = struct.unpack("!H", await reader.readexactly(2))[0]
    elif ln == 127:
        ln = struct.unpack("!Q", await reader.readexactly(8))[0]
    mask = await reader.readexactly(4) if masked else b""
    data = await reader.readexactly(ln) if ln else b""
    if masked and data:
        data = bytes(b ^ mask[i & 3] for i, b in enumerate(data))
    return opcode, data


async def _ws_handshake(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> str:
    raw = await reader.readuntil(b"\r\n\r\n")
    lines = raw.decode("latin-1").splitlines()
    path = lines[0].split(" ")[1]
    key = ""
    for line in lines[1:]:
        if line.lower().startswith("sec-websocket-key:"):
            key = line.split(":", 1)[1].strip()
    accept = base64.b64encode(
        hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
    ).decode("ascii")
    resp = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
    ).encode("ascii")
    writer.write(resp)
    await writer.drain()
    return path


class TestProfileVaultPure(unittest.TestCase):
    def test_sanitizers_block_traversal_and_bad_slots(self):
        self.assertEqual(sanitize_profile_id("work_acc-4"), "work_acc-4")
        for bad in ("", "../etc", "a/b", "a\\b", "bad space", "x" * 33):
            with self.assertRaises(ValueError):
                sanitize_profile_id(bad)

        self.assertEqual(sanitize_optional_lane_id(""), "")
        self.assertEqual(sanitize_optional_lane_id("acc4"), "acc4")
        with self.assertRaises(ValueError):
            sanitize_optional_lane_id("../acc1")

        self.assertIsNone(sanitize_optional_slot(None))
        self.assertEqual(sanitize_optional_slot(0), 0)
        self.assertEqual(sanitize_optional_slot("4"), 4)
        for bad_slot in (-1, 5, "abc"):
            with self.assertRaises(ValueError):
                sanitize_optional_slot(bad_slot)

    def test_vault_manages_many_accounts_and_never_stores_secrets(self):
        with tempfile.TemporaryDirectory() as td:
            vault = ProfileVault(Path(td))
            # Tạo 6 tài khoản công việc khác nhau (vượt xa 3 tab)
            for i in range(1, 7):
                lane = f"acc{((i - 1) % 3) + 1}"
                slot = (i - 1) // 3
                vault.upsert_profile(f"work_{i}", label=f"Work {i}", lane_id=lane, slot=slot)

            items = vault.list_profiles()
            self.assertEqual(len(items), 6)

            # Gán work_6 đè lên (acc1, slot 0) -> work_1 phải tự gỡ gán để tránh xung đột
            vault.upsert_profile("work_6", lane_id="acc1", slot=0)
            w1 = vault.get_profile("work_1")
            w6 = vault.get_profile("work_6")
            self.assertEqual(w1["lane_id"], "")
            self.assertIsNone(w1["slot"])
            self.assertEqual(w6["lane_id"], "acc1")
            self.assertEqual(w6["slot"], 0)

            # Đánh dấu thư mục đã có dữ liệu profile -> find_profile_for_slot tìm thấy
            (vault.profile_dir("work_6") / "Default" / "Network").mkdir(parents=True)
            (vault.profile_dir("work_6") / "Default" / "Network" / "Cookies").write_bytes(b"db")
            found = vault.find_profile_for_slot("acc1", 0)
            self.assertIsNotNone(found)
            self.assertEqual(found["profile_id"], "work_6")

            # Kiểm tra vault.json KHÔNG chứa bất kỳ trường bí mật nào
            raw_text = vault.vault_file.read_text(encoding="utf-8")
            self.assertNotIn("psidts", raw_text.lower())
            self.assertNotIn("password", raw_text.lower())

            # Xóa profile xóa luôn thư mục trên đĩa
            self.assertTrue(vault.delete_profile("work_6", remove_dir=True))
            self.assertFalse(vault.profile_dir("work_6").exists())
            self.assertIsNone(vault.get_profile("work_6"))

    def test_extract_gemini_cookies_prefers_valid_google_domain(self):
        now = time.time()
        cookies = [
            {"name": "__Secure-1PSID", "value": "evil_val", "domain": ".evil.com", "expires": now + 3600},
            {"name": "__Secure-1PSID", "value": "expired_val", "domain": ".google.com", "expires": now - 10},
            {"name": "__Secure-1PSID", "value": "g.a000VALID_PSID", "domain": ".google.com", "expires": now + 3600},
            {"name": "__Secure-1PSIDTS", "value": "sidts-VALID_NEW", "domain": ".google.com", "expires": now + 3600},
        ]
        psid, psidts = extract_gemini_cookies(cookies)
        self.assertEqual(psid, "g.a000VALID_PSID")
        self.assertEqual(psidts, "sidts-VALID_NEW")

    def test_extract_and_persist_full_gemini_cookie_jar_excludes_accounts_and_analytics(self):
        now = time.time()
        raw = [
            {"name": "__Secure-1PSID", "value": "g.a000PSID_X", "domain": ".google.com", "path": "/", "expires": now + 3600},
            {"name": "__Secure-1PSIDTS", "value": "sidts-TS_X", "domain": ".google.com", "path": "/", "expires": now + 3600},
            {"name": "OSID", "value": "osid_val", "domain": "gemini.google.com", "path": "/", "expires": now + 3600},
            {"name": "__Secure-OSID", "value": "sosid_val", "domain": "gemini.google.com", "path": "/", "expires": now + 3600},
            {"name": "SID", "value": "sid_val", "domain": ".google.com", "path": "/", "expires": now + 3600},
            {"name": "LSID", "value": "secret_accounts_lsid", "domain": "accounts.google.com", "path": "/", "expires": now + 3600},
            {"name": "__Host-GAPS", "value": "secret_gaps", "domain": "accounts.google.com", "path": "/", "expires": now + 3600},
            {"name": "_ga", "value": "ga123", "domain": ".gemini.google.com", "path": "/", "expires": now + 3600},
        ]
        jar = extract_gemini_cookie_jar(raw)
        names = {c["name"] for c in jar}
        self.assertEqual(names, {"__Secure-1PSID", "__Secure-1PSIDTS", "OSID", "__Secure-OSID", "SID"})

        with tempfile.TemporaryDirectory() as td:
            saved = persist_cookie_jar_to_lane_cache(Path(td), "acc2", "g.a000PSID_X", raw)
            self.assertIsNotNone(saved)
            self.assertTrue(saved.is_file())
            self.assertEqual(saved.parent.name, ".gemini_cookies_acc2")
            loaded = json.loads(saved.read_text(encoding="utf-8"))
            self.assertEqual({c["name"] for c in loaded}, names)

    def test_persist_cookies_to_lane_env_atomic(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            ok, name, err = persist_cookies_to_lane_env(
                base, "acc4", 0, "g.a000PSID4", "sidts-TS4", "Work 4"
            )
            self.assertTrue(ok)
            self.assertIsNone(err)
            self.assertEqual(name, ".env.acc4")
            txt = (base / ".env.acc4").read_text(encoding="utf-8")
            self.assertIn('SECURE_1PSID="g.a000PSID4"', txt)
            self.assertIn('SECURE_1PSIDTS="sidts-TS4"', txt)

            ok2, _, _ = persist_cookies_to_lane_env(
                base, "acc4", 1, "g.a000B1", "sidts-B1", "Backup 1"
            )
            self.assertTrue(ok2)
            txt2 = (base / ".env.acc4").read_text(encoding="utf-8")
            self.assertIn('BACKUP_1PSID="g.a000B1"', txt2)
            self.assertIn('BACKUP_1_LABEL="Backup 1"', txt2)


class TestCdpAndHeadlessSync(unittest.IsolatedAsyncioTestCase):
    async def test_cdp_connection_handles_large_fragmented_frames_ping_and_events(self):
        pong_received = asyncio.Event()
        big_blob = "X" * 70000  # > 65535 -> ép khung 64-bit length!

        async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
            try:
                await _ws_handshake(reader, writer)
                opcode, payload = await _read_client_ws_frame(reader)
                req = json.loads(payload.decode("utf-8"))
                # Gửi Ping trước -> client phải tự trả Pong
                writer.write(_encode_server_ws_frame(0x09, b"hi"))
                await writer.drain()
                p_op, p_data = await _read_client_ws_frame(reader)
                if p_op == 0x0A and p_data == b"hi":
                    pong_received.set()

                # Gửi 1 event xen kẽ trước khi trả kết quả call
                ev_bytes = json.dumps({"method": "Page.loadEventFired", "params": {"ts": 1.23}}).encode()
                writer.write(_encode_server_ws_frame(0x01, ev_bytes))

                # Gửi response lớn bị phân mảnh thành 2 frame (FIN=0 + continuation FIN=1)
                resp_bytes = json.dumps({"id": req["id"], "result": {"blob": big_blob}}).encode()
                mid = len(resp_bytes) // 2
                writer.write(_encode_server_ws_frame(0x01, resp_bytes[:mid], fin=False))
                writer.write(_encode_server_ws_frame(0x00, resp_bytes[mid:], fin=True))
                await writer.drain()
                await asyncio.sleep(0.2)
            finally:
                writer.close()

        server = await asyncio.start_server(handle_client, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with server:
            cdp = CdpConnection("127.0.0.1", port, "/devtools/browser/test")
            await cdp.connect(timeout=3.0)
            try:
                res = await cdp.call("Storage.getCookies", timeout=3.0)
                self.assertEqual(len(res["blob"]), 70000)
                self.assertTrue(pong_received.is_set())
                ev = await cdp.wait_for_event("Page.loadEventFired", timeout=1.0)
                self.assertEqual(ev, {"ts": 1.23})
            finally:
                await cdp.close()

    async def test_sync_profile_extracts_rotated_cookies_and_detects_login_redirect(self):
        browser_closed = False
        scenario = {"redirect_login": False, "get_cookies_calls": 0}

        async def mock_cdp_server(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
            nonlocal browser_closed
            try:
                path = await _ws_handshake(reader, writer)
                while True:
                    try:
                        opcode, payload = await _read_client_ws_frame(reader)
                    except asyncio.IncompleteReadError:
                        break
                    if opcode == 0x08:
                        break
                    if opcode != 0x01:
                        continue
                    req = json.loads(payload.decode("utf-8"))
                    method = req.get("method")
                    rid = req.get("id")
                    result = {}
                    if method == "Storage.getCookies":
                        scenario["get_cookies_calls"] += 1
                        ts_val = "sidts-OLD" if scenario["get_cookies_calls"] == 1 else "sidts-FRESH_ROTATED"
                        result = {
                            "cookies": [
                                {"name": "__Secure-1PSID", "value": "g.a000FRESH_PSID", "domain": ".google.com", "expires": time.time() + 3600},
                                {"name": "__Secure-1PSIDTS", "value": ts_val, "domain": ".google.com", "expires": time.time() + 3600},
                            ]
                        }
                    elif method == "Target.createTarget":
                        result = {"targetId": "PAGE123"}
                    elif method == "Page.enable" and "PAGE123" in path:
                        writer.write(_encode_server_ws_frame(0x01, json.dumps({"id": rid, "result": {}}).encode()))
                        writer.write(_encode_server_ws_frame(0x01, json.dumps({"method": "Page.loadEventFired", "params": {}}).encode()))
                        await writer.drain()
                        continue
                    elif method == "Runtime.evaluate":
                        url = (
                            "https://accounts.google.com/v3/signin/identifier"
                            if scenario["redirect_login"]
                            else "https://gemini.google.com/app"
                        )
                        result = {"result": {"value": url}}
                    elif method == "Browser.close":
                        browser_closed = True
                        result = {}
                    writer.write(_encode_server_ws_frame(0x01, json.dumps({"id": rid, "result": result}).encode()))
                    await writer.drain()
            finally:
                writer.close()

        server = await asyncio.start_server(mock_cdp_server, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        with tempfile.TemporaryDirectory() as td:
            vault = ProfileVault(Path(td))
            vault.upsert_profile("work4", label="Work 4", lane_id="acc4", slot=0)
            pdir = vault.profile_dir("work4")
            (pdir / "Default" / "Network").mkdir(parents=True)
            (pdir / "Default" / "Network" / "Cookies").write_bytes(b"sqlite")

            mgr = CookieSyncManager(vault=vault, sync_timeout=10.0)

            class FakeProc:
                def __init__(self):
                    self._rc = None

                def poll(self):
                    return self._rc

                def wait(self, timeout=None):
                    self._rc = 0
                    return 0

                def terminate(self):
                    self._rc = 0

                def kill(self):
                    self._rc = -9

            def fake_popen(cmd, **_kw):
                (pdir / "DevToolsActivePort").write_text(f"{port}\n/devtools/browser/mock\n", encoding="utf-8")
                return FakeProc()

            async with server:
                with patch("app.cookie_sync.find_browser_executable", return_value=("C:\\fake\\chrome.exe", "Chrome")), \
                     patch("app.cookie_sync.subprocess.Popen", side_effect=fake_popen):
                    # Case 1: Sync thành công + phát hiện 1PSIDTS mới rotate + gọi Browser.close
                    res = await mgr.sync_profile("work4")
                    self.assertTrue(res.ok)
                    self.assertEqual(res.psid, "g.a000FRESH_PSID")
                    self.assertEqual(res.psidts, "sidts-FRESH_ROTATED")
                    self.assertTrue(res.rotated)
                    self.assertTrue(browser_closed)
                    self.assertEqual(vault.get_profile("work4")["last_status"], "synced")

                    # Case 2: Bị redirect sang trang đăng nhập Google -> báo profile_login_required
                    scenario["redirect_login"] = True
                    scenario["get_cookies_calls"] = 0
                    res2 = await mgr.sync_profile("work4")
                    self.assertFalse(res2.ok)
                    self.assertEqual(res2.code, "profile_login_required")
                    self.assertEqual(vault.get_profile("work4")["last_status"], "login_required")

            # Case 3: Profile chưa từng đăng nhập -> báo ngay không spawn trình duyệt
            vault.upsert_profile("brand_new", lane_id="acc5", slot=0)
            res3 = await mgr.sync_profile("brand_new")
            self.assertFalse(res3.ok)
            self.assertEqual(res3.code, "profile_login_required")

    async def test_ensure_authenticated_recovers_via_auto_cookie_sync(self):
        mgr = SingleChatManager()
        mgr.state = "on"
        mgr.allow_guest = True
        mgr.chain = [{"label": "chinh", "psid": "old_psid", "psidts": "old_ts", "key": "oldkey"}]
        mgr.active_index = 0
        mgr._active_psid = "old_psid"

        # Client hiện tại bị UNAUTHENTICATED
        mgr.client = MagicMock()
        mgr.client.account_status = AccountStatus.UNAUTHENTICATED
        mgr.client._cookie_source = "Guest"
        mgr.client.init = AsyncMock(return_value=None)
        mgr.client.close = AsyncMock(return_value=None)
        mgr.client.available_models = []

        # Client mới tạo từ cookie vừa sync ngầm -> AVAILABLE
        fresh_client = MagicMock()
        fresh_client.account_status = AccountStatus.AVAILABLE
        fresh_client._cookie_source = "Base Cookies"
        fresh_client.init = AsyncMock(return_value=None)
        fresh_client.close = AsyncMock(return_value=None)
        fresh_client.available_models = []
        fresh_client.start_chat = MagicMock(return_value=MagicMock(metadata=[None] * 10))

        sync_ok = CookieSyncResult(
            ok=True,
            profile_id="acc1",
            psid="g.a000SYNCED_PSID",
            psidts="sidts-SYNCED_TS",
            account_key="syncedkey",
            rotated=True,
        )

        with patch("app.account_pool.try_auto_sync_for_worker_slot", new_callable=AsyncMock, return_value=sync_ok) as mock_sync, \
             patch("app.account_pool.persist_cookies_to_lane_env", return_value=(True, ".env.acc1", None)) as mock_persist, \
             patch("app.gemini_session.GeminiClient", return_value=fresh_client):
            ok = await mgr.ensure_authenticated(force=True)
            self.assertTrue(ok)
            self.assertTrue(mgr.auth_ok)
            self.assertEqual(mgr._active_psid, "g.a000SYNCED_PSID")
            mock_sync.assert_awaited_once()
            mock_persist.assert_called_once()

    async def test_safe_init_client_breaks_gemini_webapi_deadlock_with_auth_error(self):
        class HangingClient:
            def __init__(self):
                self.client = MagicMock(close=AsyncMock())
                self._running = True

            async def init(self, **_kw):
                await asyncio.sleep(60)

        hanging = HangingClient()
        raw_http = hanging.client
        with self.assertRaises(AuthError):
            await _safe_init_client(hanging, timeout=0.05)
        self.assertFalse(hanging._running)
        raw_http.close.assert_awaited_once()

    async def test_safe_init_client_preserves_long_stream_timeout_for_thinking_models(self):
        captured = {}

        class FastClient:
            async def init(self, **kw):
                captured.update(kw)

        await _safe_init_client(FastClient(), timeout=25.0)
        self.assertGreaterEqual(captured.get("timeout", 0), 420.0)
        self.assertGreaterEqual(captured.get("watchdog_timeout", 0), 180.0)

    def test_spawn_local_lane_strips_inherited_gateway_cookie_path_and_secrets(self):
        import os
        from app.lanes import spawn_local_lane

        captured = {}

        def fake_popen(_cmd, **kw):
            captured["env"] = dict(kw.get("env") or {})
            return MagicMock()

        with patch.dict(
            os.environ,
            {
                "GEMINI_COOKIE_PATH": "C:\\gateway\\.gemini_cookies",
                "SECURE_1PSID": "gw_psid_leak",
                "PORT": "10000",
            },
            clear=False,
        ), patch("app.lanes.subprocess.Popen", side_effect=fake_popen), \
             patch("app.lanes.Path.exists", return_value=True):
            ok = spawn_local_lane("acc1")

        self.assertTrue(ok)
        self.assertEqual(captured["env"].get("BRIDGE_INSTANCE"), "acc1")
        self.assertNotIn("GEMINI_COOKIE_PATH", captured["env"])
        self.assertNotIn("SECURE_1PSID", captured["env"])
        self.assertNotIn("PORT", captured["env"])


class TestProfileAdminEndpoints(unittest.TestCase):
    def test_profile_routes_crud_sync_and_security(self):
        with tempfile.TemporaryDirectory() as td:
            vault = ProfileVault(Path(td) / "profiles")
            env_base = Path(td) / "envbase"
            env_base.mkdir()
            mgr = CookieSyncManager(vault=vault)

            with patch("app.routes_admin.cookie_sync_manager", mgr), \
                 patch("app.routes_admin.BASE_DIR", env_base), \
                 patch.object(app_main, "API_KEY", ""), \
                 patch.object(app_main, "is_frontend_gateway", True):
                client = TestClient(app_main.app, base_url="http://127.0.0.1:10000")

                # 1. Tạo profile mới qua POST /v1/profiles
                r1 = client.post(
                    "/v1/profiles",
                    json={"profile_id": "work4", "label": "Work 4", "lane_id": "acc4", "slot": 0},
                    headers={"Origin": "http://127.0.0.1:10000"},
                )
                self.assertEqual(r1.status_code, 200)
                self.assertEqual(r1.json()["profile"]["profile_id"], "work4")

                # 2. Evil Origin -> 403
                r_evil = client.post(
                    "/v1/profiles",
                    json={"profile_id": "work5"},
                    headers={"Origin": "http://evil.example"},
                )
                self.assertEqual(r_evil.status_code, 403)

                # 3. GET /v1/profiles trả danh sách an toàn
                r_list = client.get("/v1/profiles")
                self.assertEqual(r_list.status_code, 200)
                data = r_list.json()
                self.assertEqual(len(data["profiles"]), 1)
                self.assertNotIn("psid", json.dumps(data).lower())

                # 4. Sync 1 profile khi worker acc4 đang tắt -> ghi thẳng .env.acc4 + .gemini_cookies_acc4 jar
                fake_res = CookieSyncResult(
                    ok=True,
                    profile_id="work4",
                    psid="g.a000WORK4",
                    psidts="sidts-WORK4",
                    account_key="wk4key",
                    rotated=True,
                    cookies=[
                        {"name": "__Secure-1PSID", "value": "g.a000WORK4", "domain": ".google.com", "path": "/", "expires": time.time() + 3600},
                        {"name": "OSID", "value": "osid_w4", "domain": "gemini.google.com", "path": "/", "expires": time.time() + 3600},
                    ],
                )
                with patch.object(mgr, "sync_profile", new_callable=AsyncMock, return_value=fake_res):
                    r_sync = client.post(
                        "/v1/profiles/work4/sync",
                        json={},
                        headers={"Origin": "http://127.0.0.1:10000"},
                    )
                self.assertEqual(r_sync.status_code, 200)
                self.assertEqual(r_sync.json()["delivery"]["delivery"], "env_file_only")
                self.assertIn('SECURE_1PSID="g.a000WORK4"', (env_base / ".env.acc4").read_text(encoding="utf-8"))
                self.assertTrue((env_base / ".gemini_cookies_acc4" / ".cached_cookies_g.a000WORK4.json").is_file())

                # 4b. Finish-login endpoint
                with patch.object(mgr, "finish_interactive_login", new_callable=AsyncMock, return_value=fake_res):
                    r_fin = client.post(
                        "/v1/profiles/work4/finish-login",
                        json={},
                        headers={"Origin": "http://127.0.0.1:10000"},
                    )
                self.assertEqual(r_fin.status_code, 200)
                self.assertTrue(r_fin.json()["ok"])

                # 5. Sync-all tuần tự nhiều profile
                (vault.profile_dir("work4") / "Default" / "Network").mkdir(parents=True)
                (vault.profile_dir("work4") / "Default" / "Network" / "Cookies").write_bytes(b"db")
                with patch.object(mgr, "sync_profile", new_callable=AsyncMock, return_value=fake_res):
                    r_all = client.post(
                        "/v1/profiles/sync-all",
                        json={},
                        headers={"Origin": "http://127.0.0.1:10000"},
                    )
                self.assertEqual(r_all.status_code, 200)
                self.assertEqual(r_all.json()["synced"], 1)
                self.assertEqual(r_all.json()["total"], 1)

                # 6. Yêu cầu từ IP LAN -> 403 loopback-only
                from starlette.requests import Request as StarletteRequest
                from app.routes_admin import _loopback_client_error

                lan_req = StarletteRequest(
                    {"type": "http", "headers": [], "client": ("192.168.1.55", 54321)}
                )
                lan_err = _loopback_client_error(lan_req)
                self.assertIsNotNone(lan_err)
                self.assertEqual(lan_err.status_code, 403)

                # 7. DELETE /v1/profiles/work4
                r_del = client.delete(
                    "/v1/profiles/work4",
                    headers={"Origin": "http://127.0.0.1:10000"},
                )
                self.assertEqual(r_del.status_code, 200)
                self.assertEqual(len(client.get("/v1/profiles").json()["profiles"]), 0)


if __name__ == "__main__":
    unittest.main()
