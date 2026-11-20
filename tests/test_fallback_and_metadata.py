import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app import gemini_session as gs
from app.errors import BridgeAuthError, BridgeTimeoutError
from app.session_pool import SessionSlot


class FakeChat:
    def __init__(self, metadata=None, model=None):
        self.metadata = list(metadata) if metadata else [None] * 10
        self.model = model
        self.send_message_stream = None
        self.send_message = None

    @property
    def cid(self):
        return self.metadata[0] if self.metadata else None


def _make_manager(default_model="gemini-4-hydrogen", available_models=None):
    mgr = gs.SingleChatManager.__new__(gs.SingleChatManager)
    mgr.chain = [{"label": "acc1", "psid": "p1", "psidts": "t1", "key": "k1"}]
    mgr.account_state = {}
    mgr.active_index = 0
    mgr.client = MagicMock()
    mgr.client.close = AsyncMock()
    mgr.client.init = AsyncMock()
    mgr.client.start_chat = MagicMock(
        side_effect=lambda **kw: FakeChat(
            metadata=kw.get("metadata"), model=kw.get("model")
        )
    )
    mgr.lock = asyncio.Lock()
    mgr.state = "on"
    mgr.enabled_since = 0.0
    mgr._control_lock = asyncio.Lock()
    mgr._inflight = 0
    mgr._idle_event = asyncio.Event()
    mgr._idle_event.set()
    mgr.available_models = list(available_models or ["gemini-4-hydrogen"])
    mgr.model_details = []
    mgr.default_model = default_model
    mgr.tool_header_sent = False
    mgr.chat_tool_header_hash = ""
    mgr.chat_session = FakeChat(model=default_model)
    mgr.session_initialized = False
    mgr.last_request_ts = 0.0
    mgr.turn_count = 0
    mgr.total_requests = 0
    mgr.total_errors = 0
    mgr.start_time = 0.0
    mgr._consecutive_auth_errors = 0
    mgr._consecutive_transient_errors = 0
    mgr._using_backup = False
    mgr.allow_guest = False
    mgr.is_guest = False
    mgr.auth_ok = True
    mgr.account_status_name = "AVAILABLE"
    mgr.cookie_source = "Base Cookies"
    mgr._active_psid = "p1"
    mgr._last_recover_ts = 0.0
    mgr.last_tool_header = ""
    mgr.last_turn_model = ""
    mgr.last_turn_fallback_from = None
    mgr.last_turn_prompt_chars = 0
    mgr.vision_disabled_until = 0.0
    mgr._pending_handoff = None
    mgr._fake_quota_done = False
    mgr._save_session = MagicMock()
    mgr._evaluate_auth = MagicMock(return_value=True)
    mgr._try_failover_to_backup = AsyncMock(return_value=False)
    return mgr


class TestFallbackAndMetadata(unittest.IsolatedAsyncioTestCase):
    def test_select_fallback_model_never_self_loops(self):
        mgr = _make_manager(
            default_model="gemini-4-hydrogen",
            available_models=["gemini-4-hydrogen"],
        )
        self.assertEqual(mgr._select_fallback_model(), "unspecified")

        mgr.default_model = "gemini-carbon"
        mgr.available_models = ["gemini-4-hydrogen", "gemini-carbon"]
        self.assertEqual(mgr._select_fallback_model(), "gemini-4-hydrogen")

    def test_sanitize_clears_poisoned_metadata(self):
        mgr = _make_manager()
        # Poisoned Turn 1: cid & rid set, rcid is None
        mgr.chat_session.metadata = ["c_abc", "r_123", None, None, None, None, None, None, None, None]
        mgr.turn_count = 0
        mgr.session_initialized = False

        saved = mgr._sanitize_chat_metadata_before_send()
        self.assertIsNone(saved)
        self.assertIsNone(mgr.chat_session.metadata[0])

    def test_restore_after_abort_preserves_valid_previous_turn(self):
        mgr = _make_manager()
        valid_prev = ["c_abc", "r_1", "rc_1", None, None, None, None, None, None, "ctx_1"]
        mgr.turn_count = 1
        mgr.session_initialized = True
        mgr.chat_session.metadata = ["c_abc", "r_2", None, None, None, None, None, None, None, None]

        mgr._restore_chat_metadata_after_abort(valid_prev)
        self.assertEqual(mgr.chat_session.metadata[0], "c_abc")
        self.assertEqual(mgr.chat_session.metadata[1], "r_1")
        self.assertEqual(mgr.chat_session.metadata[2], "rc_1")

    async def test_1097_fallback_checks_auth_and_rejects_guest_mode(self):
        mgr = _make_manager(default_model="gemini-4-hydrogen")
        mgr.allow_guest = False

        async def _boom_stream(_prompt):
            raise RuntimeError("Unknown API error code: 1097")
            yield  # pragma: no cover

        mgr.chat_session.send_message_stream = _boom_stream

        def _fake_eval_auth():
            mgr.auth_ok = False
            mgr.is_guest = True
            return False

        mgr._evaluate_auth = MagicMock(side_effect=_fake_eval_auth)

        events = []
        async for kind, payload in mgr._send_prepared_turn_stream_inner("hello", []):
            events.append((kind, payload))

        mgr._evaluate_auth.assert_called_once()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][0], "error")
        self.assertEqual(events[0][1]["code"], BridgeAuthError.code)

    async def test_soft_first_byte_timeout_falls_back_to_unspecified(self):
        mgr = _make_manager(default_model="gemini-4-hydrogen")
        models_used = []

        async def _slow_stream(_prompt):
            await asyncio.sleep(0.25)
            yield SimpleNamespace(text_delta="late")

        async def _fast_stream(_prompt):
            yield SimpleNamespace(text_delta="fast ok")

        def _start_chat(**kw):
            models_used.append(kw.get("model"))
            chat = FakeChat(metadata=kw.get("metadata"), model=kw.get("model"))
            chat.send_message_stream = _fast_stream
            return chat

        mgr.client.start_chat = MagicMock(side_effect=_start_chat)
        mgr.chat_session.send_message_stream = _slow_stream

        with patch.object(gs, "MODEL_FIRST_BYTE_FALLBACK_TIMEOUT", 0.05), patch.object(
            gs, "GEMINI_HARD_TIMEOUT", 20.0
        ):
            events = []
            async for kind, payload in mgr._send_prepared_turn_stream_inner("hello", []):
                events.append((kind, payload))

        self.assertEqual(mgr.default_model, "gemini-4-hydrogen")
        self.assertEqual(mgr.last_turn_model, "unspecified")
        self.assertEqual(mgr.last_turn_fallback_from, "gemini-4-hydrogen")
        self.assertEqual(models_used, [None, "gemini-4-hydrogen"])
        self.assertEqual(mgr.chat_session.model, "gemini-4-hydrogen")
        self.assertIn(("delta", "fast ok"), events)
        self.assertIn(("done", "fast ok"), events)

    async def test_soft_fallback_resets_slot_tool_header_and_reattaches_on_retry(self):
        mgr = _make_manager(
            default_model="gemini-neon",
            available_models=["gemini-neon", "gemini-4-hydrogen"],
        )
        tool_spec = "[SOFTWARE-ENGINEERING WORKSPACE]\nTOOLS (! required, ? optional):\nwrite: write a file"
        mgr.last_tool_header = tool_spec
        mgr.session_initialized = True
        mgr.tool_header_sent = True
        mgr.chat_tool_header_hash = "h1"
        mgr.turn_count = 3
        mgr.chat_session.metadata = ["c_1", "r_1", "rc_1", None, None, None, None, None, None, "ctx"]
        mgr._active_session_key = "lead-slot"
        mgr._session_pool = {
            "lead-slot": SessionSlot(
                session_key="lead-slot",
                chat_session=mgr.chat_session,
                session_initialized=True,
                turn_count=3,
                tool_header_sent=True,
                chat_tool_header_hash="h1",
                model="gemini-neon",
            )
        }

        sent_prompts = []
        slot_header_flags_on_retry = []

        async def _slow_stream(prompt):
            sent_prompts.append(("slow", prompt))
            await asyncio.sleep(0.25)
            yield SimpleNamespace(text_delta="late")

        async def _fast_stream(prompt):
            sent_prompts.append(("fallback", prompt))
            slot_header_flags_on_retry.append(mgr._session_pool["lead-slot"].tool_header_sent)
            yield SimpleNamespace(text_delta="fallback ok")

        def _start_chat(**kw):
            chat = FakeChat(metadata=kw.get("metadata"), model=kw.get("model"))
            chat.send_message_stream = _fast_stream
            return chat

        mgr.client.start_chat = MagicMock(side_effect=_start_chat)
        mgr.chat_session.send_message_stream = _slow_stream

        with patch.object(gs, "MODEL_FIRST_BYTE_FALLBACK_TIMEOUT", 0.05), patch.object(
            gs, "GEMINI_HARD_TIMEOUT", 20.0
        ):
            events = []
            async for kind, payload in mgr._send_prepared_turn_stream_inner(
                "thực hiện đi\n\n[NOTE: Reply in the EXACT SAME LANGUAGE as the USER REQUEST above.]",
                [],
                include_tool_header=True,
            ):
                events.append((kind, payload))

        self.assertEqual(len(sent_prompts), 2)
        self.assertNotIn("WORKSPACE TOOLS", sent_prompts[0][1])
        self.assertIn("=== WORKSPACE TOOLS ===", sent_prompts[1][1])
        self.assertIn(tool_spec, sent_prompts[1][1])
        self.assertEqual(slot_header_flags_on_retry, [False])
        self.assertEqual(mgr.default_model, "gemini-neon")
        self.assertEqual(mgr.chat_session.model, "gemini-neon")
        self.assertEqual(mgr.last_turn_model, "gemini-4-hydrogen")
        self.assertEqual(mgr.last_turn_fallback_from, "gemini-neon")
        self.assertTrue(mgr._session_pool["lead-slot"].tool_header_sent)
        mgr._save_session.assert_called_once()
        self.assertIn(("done", "fallback ok"), events)

    async def test_thoughts_delta_prevents_first_byte_fallback_and_exceeds_hard_timeout(self):
        mgr = _make_manager(
            default_model="gemini-neon",
            available_models=["gemini-neon", "gemini-4-hydrogen"],
        )

        async def _thinking_stream(_prompt):
            for _ in range(4):
                await asyncio.sleep(0.03)
                yield SimpleNamespace(text_delta="", thoughts_delta="thinking...")
            await asyncio.sleep(0.02)
            yield SimpleNamespace(text_delta="done ok", thoughts_delta="")

        mgr.chat_session.send_message_stream = _thinking_stream

        with patch.object(gs, "MODEL_FIRST_BYTE_FALLBACK_TIMEOUT", 0.05), patch.object(
            gs, "GEMINI_HARD_TIMEOUT", 0.08
        ), patch.object(gs, "GEMINI_CHUNK_IDLE_TIMEOUT", 0.15), patch.object(
            gs, "GEMINI_STREAM_OVERALL_TIMEOUT", 1.0
        ):
            events = []
            async for kind, payload in mgr._send_prepared_turn_stream_inner("plan game", []):
                events.append((kind, payload))

        self.assertIsNone(mgr.last_turn_fallback_from)
        self.assertEqual(mgr.last_turn_model, "gemini-neon")
        self.assertEqual(mgr.client.start_chat.call_count, 0)
        self.assertIn(("done", "done ok"), events)

    async def test_chunk_idle_timeout_after_thoughts_and_after_text(self):
        # Case A: thought emitted, then stalls past chunk idle timeout -> falls back
        mgr_a = _make_manager(
            default_model="gemini-neon",
            available_models=["gemini-neon", "gemini-4-hydrogen"],
        )

        async def _stalled_after_thought(_prompt):
            yield SimpleNamespace(text_delta="", thoughts_delta="step 1")
            await asyncio.sleep(0.5)
            yield SimpleNamespace(text_delta="late")

        async def _fallback_stream(_prompt):
            yield SimpleNamespace(text_delta="fallback ok")

        def _start_chat_a(**kw):
            c = FakeChat(metadata=kw.get("metadata"), model=kw.get("model"))
            c.send_message_stream = _fallback_stream
            return c

        mgr_a.client.start_chat = MagicMock(side_effect=_start_chat_a)
        mgr_a.chat_session.send_message_stream = _stalled_after_thought

        with patch.object(gs, "MODEL_FIRST_BYTE_FALLBACK_TIMEOUT", 0.5), patch.object(
            gs, "GEMINI_HARD_TIMEOUT", 20.0
        ), patch.object(gs, "GEMINI_CHUNK_IDLE_TIMEOUT", 0.06), patch.object(
            gs, "GEMINI_STREAM_OVERALL_TIMEOUT", 20.0
        ):
            events_a = []
            async for kind, payload in mgr_a._send_prepared_turn_stream_inner("go", []):
                events_a.append((kind, payload))

        self.assertEqual(mgr_a.last_turn_fallback_from, "gemini-neon")
        self.assertEqual(mgr_a.last_turn_model, "gemini-4-hydrogen")
        self.assertEqual(mgr_a.default_model, "gemini-neon")
        self.assertIn(("done", "fallback ok"), events_a)

        # Case B: text_delta already streamed, then stalls -> fails without retrying
        mgr_b = _make_manager(
            default_model="gemini-neon",
            available_models=["gemini-neon", "gemini-4-hydrogen"],
        )

        async def _stalled_after_text(_prompt):
            yield SimpleNamespace(text_delta="partial ")
            await asyncio.sleep(0.5)
            yield SimpleNamespace(text_delta="never")

        mgr_b.chat_session.send_message_stream = _stalled_after_text

        with patch.object(gs, "MODEL_FIRST_BYTE_FALLBACK_TIMEOUT", 0.5), patch.object(
            gs, "GEMINI_HARD_TIMEOUT", 20.0
        ), patch.object(gs, "GEMINI_CHUNK_IDLE_TIMEOUT", 0.06), patch.object(
            gs, "GEMINI_STREAM_OVERALL_TIMEOUT", 20.0
        ):
            events_b = []
            async for kind, payload in mgr_b._send_prepared_turn_stream_inner("go", []):
                events_b.append((kind, payload))

        errs_b = [p for k, p in events_b if k == "error"]
        self.assertEqual(len(errs_b), 1)
        self.assertEqual(errs_b[0]["code"], BridgeTimeoutError.code)
        self.assertIn("0.06s", errs_b[0]["message"])

    async def test_stream_overall_timeout_caps_endless_stream(self):
        mgr = _make_manager(default_model="gemini-neon")

        async def _endless_stream(_prompt):
            while True:
                await asyncio.sleep(0.03)
                yield SimpleNamespace(text_delta="x")

        mgr.chat_session.send_message_stream = _endless_stream

        with patch.object(gs, "GEMINI_CHUNK_IDLE_TIMEOUT", 0.2), patch.object(
            gs, "GEMINI_STREAM_OVERALL_TIMEOUT", 0.12
        ), patch.object(gs, "GEMINI_HARD_TIMEOUT", 0.5):
            events = []
            async for kind, payload in mgr._send_prepared_turn_stream_inner("stream", []):
                events.append((kind, payload))

        errs = [p for k, p in events if k == "error"]
        self.assertEqual(len(errs), 1)
        self.assertEqual(errs[0]["code"], BridgeTimeoutError.code)
        self.assertIn("0.12s", errs[0]["message"])


if __name__ == "__main__":
    unittest.main()
