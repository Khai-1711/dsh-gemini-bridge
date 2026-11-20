import asyncio
import json
import os
import random
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

os.environ.pop("BRIDGE_INSTANCE", None)
os.environ.setdefault("SECURE_1PSID", "mock_psid")
os.environ.setdefault("SECURE_1PSIDTS", "mock_psidts")

import main as app_main
from app import gemini_session as gs
from app.activity import record_failed_tool_trace
from app.errors import BridgeAuthError
from app.handoff import FactLedger
from app.lanes import LaneRegistry, spawn_local_lane
from app.streaming import ToolCallAwareStreamer
from app.tool_parser import has_tool_call_intent, parse_tool_calls_from_reply


class TestToolPipelineStateMachine(unittest.TestCase):
    def setUp(self):
        self.write_tools = {
            "write": {
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["file_path", "content"],
                }
            },
            "pwsh": {
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"},
                    },
                    "required": ["command"],
                }
            },
        }

    def test_embedded_close_tag_inside_json_string_is_preserved(self):
        code_body = 'def f():\n    return "<tool_call>{}</tool_call>"\n'
        payload_obj = {
            "name": "write",
            "arguments": {
                "file_path": r"D:\ABC\test_file.py",
                "content": code_body,
            },
        }
        full = (
            "Đang ghi file kiểm thử.\n"
            f"<tool_call>{json.dumps(payload_obj)}</tool_call>\n"
            "Đã xong."
        )

        streamer = ToolCallAwareStreamer()
        emitted = []
        for idx in range(0, len(full), 5):
            emitted.extend(streamer.feed(full[idx : idx + 5]))
        emitted.extend(streamer.flush())

        prose = "".join(emitted)
        self.assertEqual(prose, "Đang ghi file kiểm thử.\n\nĐã xong.")
        self.assertNotIn("test_file.py", prose)

        calls = parse_tool_calls_from_reply(streamer.full_text, self.write_tools)
        self.assertEqual(len(calls), 1)
        args = json.loads(calls[0]["function"]["arguments"])
        self.assertEqual(args["content"], code_body)

    def test_fuzz_random_chunk_splits_200_seeds(self):
        rng = random.Random(42)
        cases = [
            (
                "Alpha ",
                '<tool_call>{"name":"write","arguments":{"file_path":"D:\\\\ABC\\\\a.py","content":"x = \\"</tool_call>\\"\\n"}}</tool_call>',
                " Omega",
            ),
            (
                "Before ",
                '<tool_call>{"name":"write","arguments":{"file_path":"D:\\\\ABC\\\\b.py","content":"<tool_call>nested</tool_call>"}}</tool_call>',
                " Mid ",
                '<tool_call>{"name":"pwsh","arguments":{"command":"echo </tool_call>"}}</tool_call>',
                " End",
            ),
        ]

        for iteration in range(200):
            parts = cases[iteration % len(cases)]
            text = "".join(parts)
            expected_prose = "".join(p for p in parts if not p.startswith("<tool_call>"))

            streamer = ToolCallAwareStreamer()
            chunks = []
            pos = 0
            while pos < len(text):
                step = rng.randint(1, 7)
                chunks.extend(streamer.feed(text[pos : pos + step]))
                pos += step
            chunks.extend(streamer.flush())

            emitted = "".join(chunks)
            self.assertEqual(emitted, expected_prose, f"Fuzz fail at iter={iteration}")
            self.assertNotIn("arguments", emitted)
            calls = parse_tool_calls_from_reply(streamer.full_text, self.write_tools)
            self.assertGreaterEqual(len(calls), 1)

    def test_batch_six_tool_calls_with_one_syntax_broken_tag_rescues_all_six(self):
        valid_tags = [
            f'<tool_call>{{"name":"write","arguments":{{"file_path":"D:\\\\ABC\\\\f{i}.txt","content":"ok{i}"}}}}</tool_call>'
            for i in range(5)
        ]
        broken_sixth = (
            '<tool_call>{"name":"pwsh","arguments":{"command":"echo "hello world""}}</tool_call>'
        )
        reply = "\n".join(valid_tags + [broken_sixth])

        t0 = time.perf_counter()
        calls = parse_tool_calls_from_reply(reply, self.write_tools)
        elapsed_ms = (time.perf_counter() - t0) * 1000

        self.assertEqual(len(calls), 6)
        self.assertEqual([c["function"]["name"] for c in calls], ["write"] * 5 + ["pwsh"])
        self.assertLess(elapsed_ms, 100.0)

    def test_truncated_unclosed_tool_call_is_strictly_fail_closed(self):
        truncated = '<tool_call>{"name":"write","arguments":{"file_path":"D:\\\\ABC\\\\a.py","content":"half'
        self.assertTrue(has_tool_call_intent(truncated))
        self.assertEqual(parse_tool_calls_from_reply(truncated, self.write_tools), [])

        streamer = ToolCallAwareStreamer()
        out = "".join(streamer.feed(truncated)) + "".join(streamer.flush())
        self.assertEqual(out, "")

    def test_unclosed_fenced_tool_block_stays_fail_closed(self):
        unclosed_fence = '```tool_call\n{"name":"write","arguments":{"file_path":"D:\\\\ABC\\\\a.py","content":"x"}'
        self.assertEqual(parse_tool_calls_from_reply(unclosed_fence, self.write_tools), [])


class TestEndToEndChatCompletions(unittest.TestCase):
    def _tools_payload(self):
        return [
            {
                "type": "function",
                "function": {
                    "name": "Inspect",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "format": {"type": "string", "enum": ["summary"]},
                            "max_items": {"type": "integer"},
                        },
                        "required": ["format", "max_items"],
                        "additionalProperties": False,
                    },
                },
            }
        ]

    def test_chat_completions_stream_and_nonstream_with_deterministic_repair_and_trace_ring(self):
        with tempfile.TemporaryDirectory() as td:
            trace_file = Path(td) / "failure_traces.jsonl"

            with patch.object(app_main, "API_KEY", ""), \
                 patch.object(app_main, "FAILURE_TRACES_FILE", trace_file), \
                 patch.object(app_main.activity, "write"), \
                 patch.object(app_main.session_manager, "require_on"), \
                 patch.object(
                     app_main.session_manager,
                     "ensure_authenticated",
                     new_callable=AsyncMock,
                     return_value=True,
                 ), \
                 patch.object(
                     app_main.session_manager,
                     "activate_session_slot",
                     new_callable=AsyncMock,
                 ):
                client = TestClient(app_main.app, base_url="http://127.0.0.1:10001")

                # 1. Non-stream where syntax-broken tagged call is deterministically repaired via json-repair
                bad_single = (
                    '<tool_call>{"name":"Inspect","arguments":{"format":"summary" '
                    '"max_items":3}}</tool_call>'
                )
                with patch.object(
                    app_main.session_manager,
                    "send_prepared_turn",
                    new_callable=AsyncMock,
                    return_value=bad_single,
                ):
                    resp = client.post(
                        "/v1/chat/completions",
                        json={
                            "messages": [{"role": "user", "content": "inspect project"}],
                            "tools": self._tools_payload(),
                            "stream": False,
                        },
                    )
                self.assertEqual(resp.status_code, 200)
                data = resp.json()
                self.assertEqual(data["choices"][0]["finish_reason"], "tool_calls")
                self.assertEqual(
                    data["choices"][0]["message"]["tool_calls"][0]["function"]["name"],
                    "Inspect",
                )

                # 2. Stream truncated tool call -> fail-closed invalid_tool_call SSE + trace recorded
                async def _truncated_stream(_prompt, _images, **_kw):
                    yield "delta", "Đang kiểm tra...\n"
                    yield "delta", '<tool_call>{"name":"Inspect","arguments":{"format":"summary"'
                    yield "done", 'Đang kiểm tra...\n<tool_call>{"name":"Inspect","arguments":{"format":"summary"'

                with patch.object(
                    app_main.session_manager,
                    "send_prepared_turn_stream",
                    new=_truncated_stream,
                ):
                    stream_resp = client.post(
                        "/v1/chat/completions",
                        json={
                            "messages": [{"role": "user", "content": "inspect project"}],
                            "tools": self._tools_payload(),
                            "stream": True,
                        },
                    )
                self.assertEqual(stream_resp.status_code, 200)
                self.assertIn("invalid_tool_call", stream_resp.text)
                self.assertTrue(trace_file.exists())
                self.assertEqual(len(trace_file.read_text(encoding="utf-8").splitlines()), 1)

    def test_failure_traces_ring_buffer_caps_at_50(self):
        with tempfile.TemporaryDirectory() as td:
            trace_file = Path(td) / "failure_traces.jsonl"
            for i in range(60):
                record_failed_tool_trace(
                    trace_file,
                    request_id=f"req-{i}",
                    model="gemini-4-neon",
                    reply_text=f"bad output {i}",
                    max_entries=50,
                )
            lines = trace_file.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 50)
            self.assertEqual(json.loads(lines[0])["request_id"], "req-10")
            self.assertEqual(json.loads(lines[-1])["request_id"], "req-59")


class TestFailoverCapModelsAndConcurrency(unittest.IsolatedAsyncioTestCase):
    async def test_failover_cap_stops_cleanly_when_all_backups_fail_auth(self):
        for stream_mode in (False, True):
            mgr = gs.SingleChatManager.__new__(gs.SingleChatManager)
            mgr.chain = [
                {"label": "a1", "psid": "p1", "psidts": "t1", "key": "k1"},
                {"label": "a2", "psid": "p2", "psidts": "t2", "key": "k2"},
            ]
            mgr.account_state = {}
            mgr.active_index = 0
            mgr.lock = asyncio.Lock()
            mgr.state = "on"
            mgr._inflight = 0
            mgr._idle_event = asyncio.Event()
            mgr._idle_event.set()
            mgr.available_models = ["gemini-4-hydrogen"]
            mgr.model_details = []
            mgr.default_model = "gemini-4-hydrogen"
            mgr.tool_header_sent = False
            mgr.chat_tool_header_hash = ""
            mgr.session_initialized = False
            mgr.last_request_ts = 0.0
            mgr.turn_count = 0
            mgr.total_requests = 0
            mgr.total_errors = 0
            mgr._consecutive_auth_errors = 2
            mgr._consecutive_transient_errors = 0
            mgr.allow_guest = False
            mgr.is_guest = False
            mgr.auth_ok = True
            mgr.vision_disabled_until = 0.0
            mgr._pending_handoff = None
            mgr._fake_quota_done = False
            mgr._session_pool = {}
            mgr._active_session_key = "default"
            mgr.last_tool_header = ""
            mgr.client = MagicMock()
            mgr.client.close = AsyncMock()
            mgr.client.init = AsyncMock()
            mgr._save_session = MagicMock()
            mgr._evaluate_auth = MagicMock(return_value=False)

            auth_exc = Exception("Authentication failed: 401 unauthorized")
            mgr.chat_session = MagicMock()
            mgr.chat_session.metadata = [None] * 10
            mgr.chat_session.send_message = AsyncMock(side_effect=auth_exc)

            async def _auth_boom_stream(_prompt):
                raise auth_exc
                yield

            mgr.chat_session.send_message_stream = _auth_boom_stream
            failover_mock = AsyncMock(return_value=True)
            mgr._try_failover_to_backup = failover_mock

            if not stream_mode:
                with self.assertRaises(BridgeAuthError):
                    await mgr._send_prepared_turn_inner("hi", [])
            else:
                events = []
                async for item in mgr._send_prepared_turn_stream_inner("hi", []):
                    events.append(item)
                self.assertEqual(events[-1][0], "error")

            self.assertEqual(failover_mock.await_count, 2)

    async def test_dynamic_neon_discovery_and_dsh_switching(self):
        mgr = gs.SingleChatManager.__new__(gs.SingleChatManager)
        mgr.client = MagicMock()
        mgr.client.list_models.return_value = [
            SimpleNamespace(model_name="gemini-flash-lite", display_name="Fast", description="3.5 Flash-Lite"),
            SimpleNamespace(model_name="gemini-4-hydrogen", display_name="Hydrogen", description="4 Hydrogen"),
            SimpleNamespace(model_name="gemini-4-neon", display_name="Neon", description="4 Neon"),
            SimpleNamespace(model_name="gemini-carbon", display_name="Carbon", description="4 Carbon"),
        ]
        mgr.default_model = "unspecified"
        models = mgr._refresh_available_models()
        self.assertIn("gemini-4-neon", models)
        self.assertIn("gemini-4-hydrogen", models)
        self.assertIn("gemini-carbon", models)
        self.assertNotIn("gemini-flash-lite", models)

        mgr._ensure_default_model()
        self.assertEqual(mgr.default_model, "gemini-4-neon")
        self.assertEqual(mgr._select_fallback_model(), "gemini-4-hydrogen")

        mgr.default_model = "gemini-4-hydrogen"
        self.assertEqual(mgr._select_fallback_model(), "gemini-4-neon")

        with patch.object(app_main, "session_manager") as mock_mgr:
            mock_mgr.state = "on"
            mock_mgr.auth_ok = True
            mock_mgr.allow_guest = False
            mock_mgr.is_guest = False
            mock_mgr.available_models = ["gemini-4-hydrogen", "gemini-4-neon"]
            mock_mgr.model_details = [
                {"name": "gemini-4-neon", "display_name": "Neon", "description": "Gemini 4 Neon"},
            ]
            mock_mgr.default_model = "gemini-4-hydrogen"
            mock_mgr.switch_model = AsyncMock(side_effect=lambda m, keep_chat=True: m)

            code, data = await app_main._local_switch_model("Gemini 4 Neon")
            self.assertEqual(code, 200)
            self.assertEqual(data["active_model"], "gemini-4-neon")

    def test_fact_ledger_concurrent_saves_remain_valid_json(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = FactLedger(td)
            errors = []

            def _worker(idx: int):
                try:
                    for step in range(15):
                        ledger.note_user_goal(f"Goal worker-{idx} step-{step}")
                        ledger.note_tool_calls(
                            [
                                {
                                    "id": f"c_{idx}_{step}",
                                    "function": {
                                        "name": "write",
                                        "arguments": {"file_path": f"D:\\ABC\\f{idx}_{step}.py"},
                                    },
                                }
                            ]
                        )
                        ledger.note_tool_result(f"c_{idx}_{step}", "OK")
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=_worker, args=(i,)) for i in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(errors, [])
            parsed = json.loads(ledger.path.read_text(encoding="utf-8"))
            self.assertIn("goal", parsed)
            leftover_tmp = list(Path(td).glob("*.tmp"))
            self.assertEqual(leftover_tmp, [])

    async def test_lane_control_lock_prevents_duplicate_spawn_and_closes_log_fd(self):
        spawn_calls = []

        async def _slow_http(method, url, headers=None, body=None, timeout=2.0):
            if method == "POST" and url.endswith("/enable"):
                await asyncio.sleep(0.02)
                if len(spawn_calls) == 0:
                    return 0, {"error": "down"}
                return 200, {"state": "on", "auth_ok": True}
            return 200, {"state": "on", "auth_ok": True}

        def _spawn(lane_id):
            spawn_calls.append(lane_id)
            return True

        reg = LaneRegistry(
            self_id="default",
            peers={"acc1": "http://127.0.0.1:10001"},
            token="secret-token",
            http_call=_slow_http,
            spawn_fn=_spawn,
            is_frontend_gateway=True,
        )

        res1, res2 = await asyncio.gather(
            reg.control("acc1", "enable", AsyncMock()),
            reg.control("acc1", "enable", AsyncMock()),
        )
        self.assertEqual(res1[0], 200)
        self.assertEqual(res2[0], 200)
        self.assertEqual(len(spawn_calls), 1)

        with patch("app.lanes.subprocess.Popen") as popen_mock, \
             patch("app.lanes.open", create=True) as open_mock:
            fake_file = MagicMock()
            open_mock.return_value = fake_file
            with patch.object(Path, "exists", return_value=True):
                ok = spawn_local_lane("acc1")
            self.assertTrue(ok)
            popen_mock.assert_called_once()
            fake_file.close.assert_called_once()


class TestStickySessionPool(unittest.IsolatedAsyncioTestCase):
    def _make_pool_manager(self):
        mgr = gs.SingleChatManager.__new__(gs.SingleChatManager)
        mgr.chain = []
        mgr.lock = asyncio.Lock()
        mgr.state = "on"
        mgr._inflight = 0
        mgr._idle_event = asyncio.Event()
        mgr._idle_event.set()
        mgr.available_models = ["gemini-4-hydrogen"]
        mgr.model_details = []
        mgr.default_model = "gemini-4-hydrogen"
        mgr.tool_header_sent = False
        mgr.chat_tool_header_hash = ""
        mgr.chat_session = None
        mgr.session_initialized = False
        mgr.last_request_ts = 0.0
        mgr.turn_count = 0
        mgr.total_requests = 0
        mgr.total_errors = 0
        mgr._consecutive_auth_errors = 0
        mgr._consecutive_transient_errors = 0
        mgr.allow_guest = False
        mgr.is_guest = False
        mgr.auth_ok = True
        mgr.vision_disabled_until = 0.0
        mgr._pending_handoff = None
        mgr._fake_quota_done = False
        mgr._session_pool = {}
        mgr._active_session_key = "default"
        mgr.last_tool_header = ""
        mgr._save_session = MagicMock()
        created = []
        active_on_client = 0
        max_active_on_client = 0

        class _MockChat:
            def __init__(self, chat_id_label, initialized_cid=None):
                self._label = chat_id_label
                self.metadata = (
                    [initialized_cid, "r1", "rc1"] + [None] * 7
                    if initialized_cid
                    else [None] * 10
                )

            @property
            def cid(self):
                return self.metadata[0]

            async def send_message_stream(self, _prompt):
                nonlocal active_on_client, max_active_on_client
                active_on_client += 1
                max_active_on_client = max(max_active_on_client, active_on_client)
                await asyncio.sleep(0.01)
                active_on_client -= 1
                if not self.metadata[0]:
                    self.metadata[0] = self._label
                    self.metadata[1] = "r1"
                    self.metadata[2] = "rc1"
                yield SimpleNamespace(text_delta=f"reply-from-{self._label}")

        def _start_chat(**kw):
            meta = kw.get("metadata")
            if meta and meta[0]:
                return _MockChat(meta[0], initialized_cid=meta[0])
            idx = len(created) + 1
            chat = _MockChat(f"cid_{idx}")
            created.append(chat)
            return chat

        mgr.client = MagicMock()
        mgr.client.start_chat = MagicMock(side_effect=_start_chat)
        mgr.get_concurrency_stats = lambda: (max_active_on_client, created)
        return mgr

    async def test_lead_and_subagent_sessions_isolated_and_serialized(self):
        mgr = self._make_pool_manager()

        lead_turn1_msgs = [
            {"role": "user", "content": '<system-reminder>\nYou are the root agent. Your name is "lead".\n</system-reminder>'},
            {"role": "user", "content": "xây game cờ vây"},
        ]
        fe_turn1_msgs = [
            {"role": "user", "content": '<system-reminder>\nYour name is "agent-fe".\n</system-reminder>'},
            {"role": "user", "content": "làm giao diện"},
        ]

        lead_key, is_new = gs.compute_session_key(lead_turn1_msgs)
        self.assertTrue(is_new)
        await mgr.activate_session_slot(lead_key, is_new_conversation=True)
        events = [item async for item in mgr._send_prepared_turn_stream_inner("lead prompt", [])]
        lead_chat_1 = mgr.chat_session
        self.assertIsNotNone(lead_chat_1)
        self.assertEqual(lead_chat_1.cid, "cid_1")
        self.assertIn(("done", "reply-from-cid_1"), events)
        self.assertEqual(mgr.turn_count, 1)

        fe_key, fe_is_new = gs.compute_session_key(fe_turn1_msgs)
        self.assertTrue(fe_is_new)
        self.assertNotEqual(lead_key, fe_key)
        await mgr.activate_session_slot(fe_key, is_new_conversation=True)
        events = [item async for item in mgr._send_prepared_turn_stream_inner("fe prompt", [])]
        fe_chat_1 = mgr.chat_session
        self.assertIsNot(fe_chat_1, lead_chat_1)
        self.assertEqual(fe_chat_1.cid, "cid_2")
        self.assertIn(("done", "reply-from-cid_2"), events)

        # Subagent Turn 2 keeps fe_chat_1
        fe_turn2_msgs = fe_turn1_msgs + [{"role": "assistant", "content": "ok"}, {"role": "user", "content": "tiếp"}]
        fe_key_2, fe_is_new_2 = gs.compute_session_key(fe_turn2_msgs)
        self.assertFalse(fe_is_new_2)
        self.assertEqual(fe_key_2, fe_key)
        await mgr.activate_session_slot(fe_key_2, is_new_conversation=False)
        self.assertIs(mgr.chat_session, fe_chat_1)

        # Lead Turn 2 restores lead_chat_1 instead of clobbering either session
        lead_turn2_msgs = lead_turn1_msgs + [{"role": "assistant", "content": "done"}, {"role": "user", "content": "check"}]
        lead_key_2, lead_is_new_2 = gs.compute_session_key(lead_turn2_msgs)
        self.assertFalse(lead_is_new_2)
        self.assertEqual(lead_key_2, lead_key)
        await mgr.activate_session_slot(lead_key_2, is_new_conversation=False)
        self.assertIs(mgr.chat_session, lead_chat_1)
        self.assertEqual(mgr.turn_count, 1)

        # Two separate Turn 1 sessions both sending "hi" get distinct ChatSessions
        hi_msgs = [
            {"role": "user", "content": '<system-reminder>\nYour name is "lead".\n</system-reminder>'},
            {"role": "user", "content": "hi"},
        ]
        hi_key, hi_new = gs.compute_session_key(hi_msgs)
        await mgr.activate_session_slot(hi_key, is_new_conversation=hi_new)
        _ = [item async for item in mgr._send_prepared_turn_stream_inner("hi", [])]
        chat_hi_1 = mgr.chat_session

        await mgr.activate_session_slot(hi_key, is_new_conversation=True)
        _ = [item async for item in mgr._send_prepared_turn_stream_inner("hi", [])]
        chat_hi_2 = mgr.chat_session
        self.assertIsNot(chat_hi_1, chat_hi_2)

        # Sequential lock enforcement across concurrent stream tasks
        async def _consume(prompt):
            return [item async for item in mgr._send_prepared_turn_stream_inner(prompt, [])]

        await asyncio.gather(_consume("p1"), _consume("p2"))
        max_active, _ = mgr.get_concurrency_stats()
        self.assertEqual(max_active, 1)

    def test_compute_session_key_stable_across_dynamic_system_reminder_and_time_context(self):
        msgs_turn1 = [
            {
                "role": "user",
                "content": '<system-reminder>\nYour name is "lead".\nElapsed: 1s\n</system-reminder>\n'
                           'Time sampled while preparing turn 1, step 1: 2026-11-19T23:50:00+09:00',
            },
            {"role": "user", "content": "  tạo   game cờ vây  "},
        ]
        msgs_turn2 = [
            {
                "role": "user",
                "content": '<system-reminder>\nYour name is "lead".\nElapsed: 99s\nCWD changed\n</system-reminder>\n'
                           'Time sampled while preparing turn 2, step 1: 2026-11-19T23:55:00+09:00',
            },
            {"role": "user", "content": "tạo game cờ vây"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "tiếp"},
        ]
        k1, new1 = gs.compute_session_key(msgs_turn1)
        k2, new2 = gs.compute_session_key(msgs_turn2)
        self.assertTrue(new1)
        self.assertFalse(new2)
        self.assertEqual(k1, k2)


if __name__ == "__main__":
    unittest.main()
