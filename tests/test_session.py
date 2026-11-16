import asyncio
import json
import logging
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

# Silence bridge logger during test runs
logging.disable(logging.CRITICAL)
logging.raiseExceptions = False

from gemini_webapi.constants import AccountStatus
from app.errors import (
    BridgeAuthError,
    BridgeBusyError,
    BridgeError,
    BridgeTimeoutError,
    BridgeUpstreamError,
)
from app.gemini_session import (
    SingleChatManager,
    load_saved_session,
    save_session_state,
)


class MockChatSession:
    def __init__(self, metadata=None):
        self.metadata = list(metadata) if metadata else []
        self.send_message = AsyncMock()


class MockClient:
    def __init__(self, account_status=AccountStatus.AVAILABLE, cookie_source="Base Cookies"):
        self.account_status = account_status
        self._cookie_source = cookie_source
        self.model_names = ["models/gemini-2.5-pro"]
        self.metadata = ["cid_123", "rid_456"]

    async def init(self, **kwargs):
        pass

    async def close(self):
        pass

    def resolve_model(self, name):
        class M:
            model_name = name

        return M()

    def start_chat(self, **kwargs):
        meta = kwargs.get("metadata", [])
        return MockChatSession(metadata=meta)


def make_manager(
    account_status=AccountStatus.AVAILABLE,
    cookie_source="Base Cookies",
    psid="test_psid",
) -> SingleChatManager:
    mgr = SingleChatManager()
    mgr.state = "on"
    mgr.client = MockClient(account_status, cookie_source)
    mgr._active_psid = psid
    mgr.default_model = "models/gemini-2.5-pro"
    mgr.chat_session = mgr.client.start_chat()
    mgr._evaluate_auth()
    return mgr


async def _collect_stream_items(stream):
    return [item async for item in stream]


class TestSessionManager(unittest.TestCase):
    def test_evaluate_auth_available(self):
        mgr = make_manager(AccountStatus.AVAILABLE, "Base Cookies")
        self.assertTrue(mgr._evaluate_auth())
        self.assertTrue(mgr.auth_ok)
        self.assertEqual(mgr.account_status_name, "AVAILABLE")
        self.assertEqual(mgr.cookie_source, "Base Cookies")

    def test_evaluate_auth_guest_rejected(self):
        mgr = make_manager(AccountStatus.AVAILABLE, "Guest")
        self.assertFalse(mgr._evaluate_auth())
        self.assertFalse(mgr.auth_ok)

    def test_evaluate_auth_invalid(self):
        mgr = make_manager(AccountStatus.UNAUTHENTICATED, "Base Cookies")
        self.assertFalse(mgr._evaluate_auth())
        self.assertFalse(mgr.auth_ok)
        self.assertEqual(mgr.account_status_name, "UNAUTHENTICATED")

    def test_guest_mode_enabled_and_ready(self):
        mgr = make_manager(AccountStatus.UNAUTHENTICATED, "Guest")
        self.assertTrue(mgr.is_guest)
        self.assertFalse(mgr.auth_ok)
        self.assertTrue(mgr.allow_guest)
        loop = asyncio.new_event_loop()
        try:
            self.assertTrue(loop.run_until_complete(mgr.ensure_authenticated()))
        finally:
            loop.close()

    def test_session_state_bound_to_account(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            session_file = Path(tmp_dir) / ".bridge_session.json"
            with patch("app.gemini_session.SESSION_STATE_FILE", session_file):
                mgr_a = make_manager(psid="cookie_psid_AAA")
                key_a = mgr_a.account_key
                save_session_state(
                    ["meta1", "meta2"],
                    "models/gemini-2.5-pro",
                    account_key=key_a,
                )

                # Nạp file đã lưu và kiểm tra account_key
                loaded = load_saved_session()
                self.assertIsNotNone(loaded)
                self.assertEqual(loaded["metadata"], ["meta1", "meta2"])
                self.assertEqual(loaded["account_key"], key_a)

                # Cùng account_key -> khớp
                self.assertEqual(loaded.get("account_key"), mgr_a.account_key)

                # Khác account_key -> không khớp
                mgr_b = make_manager(psid="cookie_psid_BBB")
                self.assertNotEqual(loaded.get("account_key"), mgr_b.account_key)

    def test_tool_header_injected_only_when_needed(self):
        mgr = make_manager()
        mgr.remember_tool_header("SYSTEM_TOOL_HEADER_XYZ")
        mgr.session_initialized = False
        mgr.tool_header_sent = False

        # Turn 1: chưa gửi header -> prompt được gắn header
        p1 = mgr._ensure_header_in_prompt("Hello Turn 1")
        self.assertIn("SYSTEM_TOOL_HEADER_XYZ", p1)
        self.assertIn("Hello Turn 1", p1)

        # Đánh dấu đã gửi (khi lượt thành công)
        mgr.session_initialized = True
        mgr.tool_header_sent = True

        # Turn 2: đã gửi header -> prompt không bị gắn lại
        p2 = mgr._ensure_header_in_prompt("Hello Turn 2")
        self.assertNotIn("SYSTEM_TOOL_HEADER_XYZ", p2)
        self.assertEqual(p2, "Hello Turn 2")

        # Giả lập reconnect / failover: tool_header_sent reset về False
        mgr.tool_header_sent = False
        p3 = mgr._ensure_header_in_prompt("Hello Turn 3")
        self.assertIn("SYSTEM_TOOL_HEADER_XYZ", p3)

    def test_metadata_preserved_on_retry(self):
        mgr = make_manager()
        mgr.chat_session = MockChatSession(
            metadata=["cid_preserved", "rid_preserved"]
        )
        saved_meta = mgr._chat_metadata_snapshot()
        self.assertEqual(saved_meta, ["cid_preserved", "rid_preserved"])

        mgr._create_new_chat_session(preserve_metadata=saved_meta)
        self.assertEqual(
            mgr.chat_session.metadata, ["cid_preserved", "rid_preserved"]
        )

    def test_vision_temporary_disable(self):
        mgr = make_manager()
        self.assertTrue(mgr.vision_upload_available)

        # Đặt vision_disabled_until vào 300 giây tương lai
        mgr.vision_disabled_until = time.time() + 300
        self.assertFalse(mgr.vision_upload_available)

        # Hết hạn -> khả dụng trở lại
        mgr.vision_disabled_until = time.time() - 1
        self.assertTrue(mgr.vision_upload_available)

    def test_image_request_deadline_covers_upload_and_fallback(self):
        mgr = make_manager()

        async def slow_send(*args, **kwargs):
            await asyncio.sleep(1)

        mgr.chat_session.send_message = AsyncMock(side_effect=slow_send)

        with patch("app.gemini_session.IMAGE_REQUEST_TIMEOUT", 0.03), \
             patch("app.gemini_session.GEMINI_HARD_TIMEOUT", 5), \
             patch("app.gemini_session.MIN_REQUEST_INTERVAL", 0), \
             patch.object(mgr, "_save_session"):
            with self.assertRaises(BridgeTimeoutError) as cm:
                asyncio.run(mgr.send_prepared_turn("analyze screenshot", ["image.png"]))

        self.assertEqual(cm.exception.code, BridgeTimeoutError.code)

    def test_image_request_uses_local_ocr_fallback(self):
        mgr = make_manager()

        async def send_message(prompt, files=None):
            if files:
                raise RuntimeError("mock vision upload failure")
            self.assertIn("recognized screenshot text", prompt)
            return SimpleNamespace(text="OCR fallback answer")

        mgr.chat_session.send_message = AsyncMock(side_effect=send_message)
        with patch("app.gemini_session.IMAGE_REQUEST_TIMEOUT", 2), \
             patch("app.gemini_session.MIN_REQUEST_INTERVAL", 0), \
             patch(
                 "app.gemini_session.extract_text_from_image_local",
                 return_value="recognized screenshot text",
             ), \
             patch.object(mgr, "_save_session"):
            reply = asyncio.run(
                mgr.send_prepared_turn("analyze screenshot", ["image.png"])
            )

        self.assertEqual(reply, "OCR fallback answer")
        self.assertEqual(mgr.chat_session.send_message.await_count, 2)

    def test_image_fallback_has_remaining_deadline(self):
        mgr = make_manager()

        async def send_message(prompt, files=None):
            if files:
                await asyncio.sleep(0.07)
                return SimpleNamespace(text="late direct-image response")
            await asyncio.sleep(0.01)
            return SimpleNamespace(text="OCR fallback response")

        mgr.chat_session.send_message = send_message
        with patch("app.gemini_session.IMAGE_REQUEST_TIMEOUT", 0.12), \
             patch("app.gemini_session.GEMINI_HARD_TIMEOUT", 1), \
             patch("app.gemini_session.MIN_REQUEST_INTERVAL", 0), \
             patch(
                 "app.gemini_session.extract_text_from_image_local",
                 return_value="recognized screenshot text",
             ), \
             patch.object(mgr, "_save_session"):
            reply = asyncio.run(
                mgr.send_prepared_turn(
                    "Analyze the screenshot and report the visible issue.",
                    ["image.png"],
                )
            )

        self.assertEqual(reply, "OCR fallback response")

    def test_stream_image_timeout_is_structured(self):
        mgr = make_manager()

        async def slow_stream(prompt, files=None):
            await asyncio.sleep(1)
            yield SimpleNamespace(text_delta="too late")

        mgr.chat_session.send_message_stream = slow_stream
        with patch("app.gemini_session.IMAGE_REQUEST_TIMEOUT", 0.03), \
             patch("app.gemini_session.GEMINI_HARD_TIMEOUT", 5), \
             patch("app.gemini_session.MIN_REQUEST_INTERVAL", 0):
            results = asyncio.run(_collect_stream_items(
                mgr.send_prepared_turn_stream("analyze screenshot", ["image.png"])
            ))

        errors = [payload for kind, payload in results if kind == "error"]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["code"], BridgeTimeoutError.code)

    def test_stream_image_deadline_includes_ocr_fallback(self):
        mgr = make_manager()
        send_calls = []

        async def send_stream(prompt, files=None):
            send_calls.append(bool(files))
            if files:
                raise RuntimeError("mock vision upload failure")
            yield SimpleNamespace(text_delta="unexpected text fallback")

        def slow_ocr(image_path):
            time.sleep(0.1)
            return "recognized screenshot text"

        mgr.chat_session.send_message_stream = send_stream
        with patch("app.gemini_session.IMAGE_REQUEST_TIMEOUT", 0.03), \
             patch("app.gemini_session.MIN_REQUEST_INTERVAL", 0), \
             patch("app.gemini_session.extract_text_from_image_local", side_effect=slow_ocr):
            results = asyncio.run(_collect_stream_items(
                mgr.send_prepared_turn_stream(
                    "Analyze the screenshot and report the visible issue.",
                    ["image.png"],
                )
            ))

        errors = [payload for kind, payload in results if kind == "error"]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["code"], BridgeTimeoutError.code)
        self.assertEqual(send_calls, [True])

    def test_stream_image_fallback_has_remaining_deadline(self):
        mgr = make_manager()

        async def send_stream(prompt, files=None):
            if files:
                await asyncio.sleep(0.07)
                yield SimpleNamespace(text_delta="late direct-image response")
            else:
                await asyncio.sleep(0.01)
                yield SimpleNamespace(text_delta="OCR fallback response")

        mgr.chat_session.send_message_stream = send_stream
        with patch("app.gemini_session.IMAGE_REQUEST_TIMEOUT", 0.12), \
             patch("app.gemini_session.GEMINI_HARD_TIMEOUT", 1), \
             patch("app.gemini_session.MIN_REQUEST_INTERVAL", 0), \
             patch(
                 "app.gemini_session.extract_text_from_image_local",
                 return_value="recognized screenshot text",
             ), \
             patch.object(mgr, "_save_session"):
            results = asyncio.run(_collect_stream_items(
                mgr.send_prepared_turn_stream(
                    "Analyze the screenshot and report the visible issue.",
                    ["image.png"],
                )
            ))

        self.assertIn(("delta", "OCR fallback response"), results)
        self.assertIn(("done", "OCR fallback response"), results)
        self.assertNotIn(("delta", "late direct-image response"), results)

    def test_stream_image_uses_local_ocr_fallback(self):
        mgr = make_manager()

        async def send_stream(prompt, files=None):
            if files:
                raise RuntimeError("mock vision upload failure")
            self.assertIn("recognized screenshot text", prompt)
            yield SimpleNamespace(text_delta="OCR fallback answer")

        mgr.chat_session.send_message_stream = send_stream
        with patch("app.gemini_session.IMAGE_REQUEST_TIMEOUT", 2), \
             patch("app.gemini_session.MIN_REQUEST_INTERVAL", 0), \
             patch(
                 "app.gemini_session.extract_text_from_image_local",
                 return_value="recognized screenshot text",
             ), \
             patch.object(mgr, "_save_session"):
            results = asyncio.run(_collect_stream_items(
                mgr.send_prepared_turn_stream("analyze screenshot", ["image.png"])
            ))

        self.assertIn(("delta", "OCR fallback answer"), results)
        self.assertIn(("done", "OCR fallback answer"), results)

    def test_error_classes_and_status_codes(self):
        auth_err = BridgeAuthError("Auth error")
        self.assertEqual(auth_err.status_code, 503)
        self.assertEqual(auth_err.err_type, "authentication_error")
        self.assertEqual(auth_err.code, "gemini_not_authenticated")

        busy_err = BridgeBusyError("Busy")
        self.assertEqual(busy_err.status_code, 429)
        self.assertEqual(busy_err.err_type, "rate_limit_error")
        self.assertEqual(busy_err.code, "bridge_busy")

        timeout_err = BridgeTimeoutError("Timeout")
        self.assertEqual(timeout_err.status_code, 504)
        self.assertEqual(timeout_err.err_type, "timeout_error")
        self.assertEqual(timeout_err.code, "gemini_timeout")

        upstream_err = BridgeUpstreamError("Upstream")
        self.assertEqual(upstream_err.status_code, 502)
        self.assertEqual(upstream_err.err_type, "upstream_error")
        self.assertEqual(upstream_err.code, "gemini_upstream_error")

    def test_get_status_reflects_auth(self):
        mgr = make_manager(AccountStatus.AVAILABLE, "Base Cookies")
        st = mgr.get_status()
        self.assertEqual(st["status"], "ok")
        self.assertTrue(st["auth_ok"])
        self.assertEqual(st["account_status"], "AVAILABLE")
        self.assertEqual(st["cookie_source"], "Base Cookies")

        mgr_guest = make_manager(AccountStatus.AVAILABLE, "Guest")
        st_guest = mgr_guest.get_status()
        self.assertEqual(st_guest["status"], "degraded")
        self.assertFalse(st_guest["auth_ok"])
        self.assertEqual(st_guest["cookie_source"], "Guest")


if __name__ == "__main__":
    unittest.main()
