import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Pop BRIDGE_INSTANCE trước khi import app.*
os.environ.pop("BRIDGE_INSTANCE", None)
os.environ.setdefault("SECURE_1PSID", "mock_psid")
os.environ.setdefault("SECURE_1PSIDTS", "mock_psidts")

from gemini_webapi.constants import AccountStatus
from app.control import is_origin_allowed, parse_start_state
from app.errors import (
    BridgeControlError,
    BridgeDisabledError,
    BridgeUpstreamError,
)
from app.gemini_session import SingleChatManager, save_session_state
from starlette.requests import Request


class MockClient:
    def __init__(self, init_delay: float = 0.0, fail_init: bool = False):
        self.init_calls = 0
        self.close_calls = 0
        self.init_delay = init_delay
        self.fail_init = fail_init
        self.account_status = AccountStatus.AVAILABLE
        self._cookie_source = "test"
        self.models = []

    async def init(self, **kwargs):
        self.init_calls += 1
        if self.init_delay > 0:
            await asyncio.sleep(self.init_delay)
        if self.fail_init:
            raise RuntimeError("Mạng lỗi không kết nối được Gemini Web")

    async def close(self):
        self.close_calls += 1

    def list_models(self):
        return []

    def start_chat(self, **kwargs):
        chat = MagicMock()
        chat.metadata = ["test_cid", "test_rid"]
        chat.send_message = AsyncMock(return_value=MagicMock(text="test reply"))
        return chat


class TestControlPure(unittest.TestCase):
    def test_parse_start_state(self):
        # Truthy values
        for val in ("on", "ON", "On", "true", "TRUE", "1", "yes", "YES"):
            self.assertTrue(parse_start_state(val), f"Failed for {val}")
        # Falsy values
        for val in ("", "off", "OFF", "0", "false", "no", "abc", None):
            self.assertFalse(parse_start_state(val), f"Failed for {val}")

    def test_is_origin_allowed(self):
        # None (curl, scripts, DSH) -> True
        self.assertTrue(is_origin_allowed(None, 10001))
        # Localhost & 127.0.0.1 on matching port -> True
        self.assertTrue(is_origin_allowed("http://127.0.0.1:10001", 10001))
        self.assertTrue(is_origin_allowed("http://localhost:10001", 10001))
        # Wrong port -> False
        self.assertFalse(is_origin_allowed("http://127.0.0.1:10000", 10001))
        # External domain -> False
        self.assertFalse(is_origin_allowed("http://evil.example", 10001))
        self.assertFalse(is_origin_allowed("http://evil.example:10001", 10001))
        # HTTPS -> False (bridge uses HTTP)
        self.assertFalse(is_origin_allowed("https://127.0.0.1:10001", 10001))
        # LAN private IP on matching port (e.g. 2 computers setup) -> True
        self.assertTrue(is_origin_allowed("http://192.168.1.50:10001", 10001))
        self.assertTrue(is_origin_allowed("http://10.0.0.2:10001", 10001))


class TestSingleChatManagerControl(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.session_file = Path(self.td.name) / ".bridge_session.json"

    def tearDown(self):
        self.td.cleanup()

    def _make_manager(self, fail_init=False, init_delay=0.0) -> SingleChatManager:
        m = SingleChatManager()
        m.chain = []
        m.client = MockClient(init_delay=init_delay, fail_init=fail_init)
        return m

    def test_starts_off_without_init(self):
        m = self._make_manager()
        self.assertEqual(m.state, "off")
        self.assertIsNone(m.enabled_since)
        self.assertEqual(m.client.init_calls, 0)
        self.assertEqual(m.client.close_calls, 0)

    def test_require_on_when_off(self):
        m = self._make_manager()
        with self.assertRaises(BridgeDisabledError) as cm:
            m.require_on()
        self.assertEqual(cm.exception.status_code, 503)
        self.assertEqual(cm.exception.code, "bridge_disabled")

    async def test_enable_idempotent(self):
        m = self._make_manager()
        await m.enable()
        self.assertEqual(m.state, "on")
        self.assertIsNotNone(m.enabled_since)
        self.assertEqual(m.client.init_calls, 1)

        # Enable lần thứ 2: không init lại
        await m.enable()
        self.assertEqual(m.state, "on")
        self.assertEqual(m.client.init_calls, 1)

    async def test_enable_failure_returns_to_off(self):
        m = self._make_manager(fail_init=True)
        with self.assertRaises(BridgeUpstreamError):
            await m.enable()
        self.assertEqual(m.state, "off")
        self.assertIsNone(m.enabled_since)

        # Sửa client thành công và bật lại
        m.client.fail_init = False
        await m.enable()
        self.assertEqual(m.state, "on")

    async def test_disable_idle(self):
        m = self._make_manager()
        await m.enable()
        self.assertEqual(m.state, "on")

        # Lưu session state mẫu
        with patch("app.gemini_session.SESSION_STATE_FILE", self.session_file):
            save_session_state(["cid_abc"], "gemini-flash", m.account_key)
            self.assertTrue(self.session_file.exists())

            await m.disable()
            self.assertEqual(m.state, "off")
            self.assertIsNone(m.enabled_since)
            self.assertEqual(m.client.close_calls, 1)
            # File session state vẫn còn nguyên
            self.assertTrue(self.session_file.exists())

    async def test_disable_waits_for_inflight(self):
        m = self._make_manager()
        await m.enable()

        gate = asyncio.Event()
        request_completed = False

        async def fake_request():
            nonlocal request_completed
            m.require_on()
            m._inflight += 1
            m._idle_event.clear()
            try:
                await gate.wait()
                request_completed = True
            finally:
                m._inflight -= 1
                if m._inflight <= 0:
                    m._inflight = 0
                    m._idle_event.set()

        req_task = asyncio.create_task(fake_request())
        await asyncio.sleep(0.01)

        self.assertEqual(m._inflight, 1)

        disable_task = asyncio.create_task(m.disable())
        await asyncio.sleep(0.01)

        # Lúc này state đang là 'stopping', request mới bị từ chối
        self.assertEqual(m.state, "stopping")
        with self.assertRaises(BridgeDisabledError):
            m.require_on()

        # Disable CHƯA xong vì req_task đang chờ gate
        self.assertFalse(disable_task.done())

        # Thả gate cho request kết thúc
        gate.set()
        await req_task
        await disable_task

        self.assertTrue(request_completed)
        self.assertEqual(m.state, "off")
        self.assertEqual(m.client.close_calls, 1)

    async def test_disable_drain_timeout(self):
        m = self._make_manager()
        await m.enable()

        gate = asyncio.Event()

        async def hung_request():
            m.require_on()
            m._inflight += 1
            m._idle_event.clear()
            try:
                await gate.wait()
            finally:
                m._inflight -= 1
                if m._inflight <= 0:
                    m._inflight = 0
                    m._idle_event.set()

        task = asyncio.create_task(hung_request())
        await asyncio.sleep(0.01)

        with patch("app.gemini_session.DISABLE_DRAIN_TIMEOUT", 0.05):
            with self.assertRaises(BridgeControlError) as cm:
                await m.disable()
            self.assertEqual(cm.exception.status_code, 409)
            # Không ép tắt: bridge vẫn giữ trạng thái ON
            self.assertEqual(m.state, "on")
            self.assertEqual(m.client.close_calls, 0)

        gate.set()
        await task

    async def test_concurrent_control_conflict(self):
        m = self._make_manager(init_delay=0.1)

        # Gọi enable() (đang chạy chậm)
        enable_task = asyncio.create_task(m.enable())
        await asyncio.sleep(0.01)

        # Gọi disable() trong lúc enable() đang giữ _control_lock
        with self.assertRaises(BridgeControlError) as cm:
            await m.disable()
        self.assertEqual(cm.exception.status_code, 409)

        await enable_task
        self.assertEqual(m.state, "on")

    async def test_no_autorecovery_when_off(self):
        m = self._make_manager()
        self.assertEqual(m.state, "off")

        auth_res = await m.ensure_authenticated()
        self.assertFalse(auth_res)
        self.assertEqual(m.client.init_calls, 0)

        failover_res = await m._try_failover_to_backup()
        self.assertFalse(failover_res)
        self.assertEqual(m.client.init_calls, 0)

    def test_status_when_off(self):
        m = self._make_manager()
        self.assertEqual(m.state, "off")
        st = m.get_status()
        self.assertEqual(st["state"], "off")
        self.assertFalse(st["enabled"])
        self.assertIsNone(st["auth_ok"])
        self.assertIsNone(st["account_status"])
        self.assertIsNone(st["cookie_source"])

    async def test_update_cookies_when_off(self):
        m = self._make_manager()
        self.assertEqual(m.state, "off")
        with patch("app.gemini_session.GeminiClient") as mock_cls:
            mock_inst = MockClient()
            mock_cls.return_value = mock_inst
            res = await m.update_cookies("new_psid_val", "new_psidts_val")
            self.assertEqual(res, {"status": "saved_while_off"})
            self.assertEqual(m._active_psid, "new_psid_val")
            self.assertEqual(mock_inst.init_calls, 0)


class TestControlEndpoints(unittest.IsolatedAsyncioTestCase):
    async def test_control_and_chat_endpoints_when_off(self):
        from main import (
            chat_completions,
            control_disable,
            control_enable,
            control_status,
            list_models,
            reset_chat_session,
            session_manager,
            switch_model,
        )

        # Đảm bảo bridge đang TẮT
        if session_manager.state == "on":
            await session_manager.disable()
        session_manager.state = "off"

        # 1. GET /v1/control trả thông tin chuẩn
        status_resp = await control_status()
        self.assertEqual(status_resp["state"], "off")
        self.assertFalse(status_resp["enabled"])

        # 2. POST /v1/chat/completions khi TẮT -> 503 bridge_disabled
        req_chat = Request({"type": "http", "headers": []})
        req_chat._json = {"messages": [{"role": "user", "content": "hi"}], "stream": True}
        resp = await chat_completions(req_chat)
        self.assertEqual(resp.status_code, 503)
        body = json.loads(resp.body)
        self.assertEqual(body["error"]["code"], "bridge_disabled")

        # 3. /v1/models khi TẮT -> 503
        resp_models = await list_models()
        self.assertEqual(resp_models.status_code, 503)

        # 4. /v1/reset khi TẮT -> 503
        resp_reset = await reset_chat_session()
        self.assertEqual(resp_reset.status_code, 503)

        # 5. /v1/switch-model khi TẮT -> 503
        req_sw = Request({"type": "http", "headers": []})
        req_sw._json = {"model": "gemini-flash"}
        resp_sw = await switch_model(req_sw)
        self.assertEqual(resp_sw.status_code, 503)

        # 6. POST /v1/control/enable với Origin lạ -> 403 origin_not_allowed
        req_evil = Request({"type": "http", "headers": [(b"origin", b"http://evil.example")]})
        resp_evil = await control_enable(req_evil)
        self.assertEqual(resp_evil.status_code, 403)
        body_evil = json.loads(resp_evil.body)
        self.assertEqual(body_evil["error"]["code"], "origin_not_allowed")

        # 7. POST /v1/control/disable với Origin lạ -> 403
        resp_dis_evil = await control_disable(req_evil)
        self.assertEqual(resp_dis_evil.status_code, 403)


if __name__ == "__main__":
    unittest.main()
