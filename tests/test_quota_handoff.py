import json
import logging
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.pop("BRIDGE_INSTANCE", None)
logging.disable(logging.CRITICAL)

from gemini_webapi.constants import AccountStatus
from gemini_webapi.exceptions import UsageLimitExceededError

from app.accounts import (
    build_account_chain,
    find_chain_overlap,
    is_available,
    load_state,
    mark_exhausted,
    next_available_index,
    save_state,
    write_chain_file,
)
from app.errors import BridgeQuotaError
import app.gemini_session as gs
from app.handoff import (
    FactLedger,
    bound_sections,
    build_handoff_message,
    parse_sections,
)


class TestPureQuotaHandoff(unittest.TestCase):
    def test_build_account_chain(self):
        env = {
            "INSTANCE_LABEL": "primary",
            "SECURE_1PSID": "psid_main",
            "SECURE_1PSIDTS": "ts_main",
            "BACKUP_1_LABEL": "backup_1",
            "BACKUP_1PSID": "psid_b1",
            "BACKUP_1PSIDTS": "ts_b1",
            "BACKUP_2_LABEL": "backup_2",
            "BACKUP_2PSID": "psid_main",  # Duplicate of main
            "BACKUP_2PSIDTS": "ts_diff",
            "BACKUP_3_LABEL": "backup_3",
            "BACKUP_3PSID": "psid_b3",
            "BACKUP_3PSIDTS": "ts_b3",
        }
        chain = build_account_chain(env)
        self.assertEqual(len(chain), 3)
        self.assertEqual(chain[0]["label"], "primary")
        self.assertEqual(chain[0]["psid"], "psid_main")
        self.assertEqual(chain[1]["label"], "backup_1")
        self.assertEqual(chain[1]["psid"], "psid_b1")
        self.assertEqual(chain[2]["label"], "backup_3")
        self.assertEqual(chain[2]["psid"], "psid_b3")

    def test_chain_without_backup(self):
        env = {"SECURE_1PSID": "psid_only", "SECURE_1PSIDTS": "ts_only"}
        chain = build_account_chain(env)
        self.assertEqual(len(chain), 1)
        self.assertEqual(chain[0]["label"], "chinh")
        self.assertEqual(chain[0]["psid"], "psid_only")

    def test_state_roundtrip_and_expiry(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = Path(td) / "accounts_state.json"
            state = {}
            now = 1000.0
            mark_exhausted(state, "acc_key_1", now + 100, "quota", now)
            save_state(state_file, state)

            loaded = load_state(state_file)
            self.assertIn("acc_key_1", loaded)
            self.assertFalse(is_available(loaded, "acc_key_1", now + 50))
            self.assertTrue(is_available(loaded, "acc_key_1", now + 150))
            self.assertTrue(is_available(loaded, "acc_key_2", now + 50))

    def test_next_available_index(self):
        chain = [
            {"key": "k0", "label": "a0"},
            {"key": "k1", "label": "a1"},
            {"key": "k2", "label": "a2"},
        ]
        state = {}
        now = 1000.0
        # If all ready, next from 0 is 1
        self.assertEqual(next_available_index(chain, state, 0, now), 1)
        # Next from 2 wraps around to 0
        self.assertEqual(next_available_index(chain, state, 2, now), 0)

        # If k1 is resting, next from 0 skips 1 to 2
        mark_exhausted(state, "k1", now + 100, "quota", now)
        self.assertEqual(next_available_index(chain, state, 0, now), 2)

        # If all resting, returns None
        mark_exhausted(state, "k0", now + 100, "quota", now)
        mark_exhausted(state, "k2", now + 100, "quota", now)
        self.assertIsNone(next_available_index(chain, state, 0, now))

    def test_parse_and_bound_limits(self):
        text = (
            "[GOAL]\n"
            "Build robust system\n"
            "[DONE]\n"
            + "\n".join(
                f"- Long completed task {i} with a large amount of extra words and characters"
                for i in range(15)
            )
            + "\n[DOING]\n"
            "- Finishing up authentication\n"
            "[NEXT]\n"
            "- Next step item 1\n"
            "[DECISIONS]\n"
            "- Decision 1\n"
            "[FILES]\n"
            + "\n".join(f"- D:\\project\\subpath\\file_{i}.py" for i in range(30))
            + "\n[LAST_ERROR]\n"
            "- No errors encountered\n"
        )
        sections = parse_sections(text)
        bounded = bound_sections(sections, max_chars=2500)
        self.assertLessEqual(len(bounded), 2500)
        self.assertIn("(+", bounded)
        self.assertIn("xem archive", bounded)

    def test_bound_keeps_core_sections(self):
        text = (
            "[GOAL]\nSmall goal\n"
            "[DONE]\n- Done A\n- Done B\n"
            "[DOING]\n- Doing C\n"
            "[NEXT]\n- Next D\n"
            "[DECISIONS]\n- Decided E\n"
            "[FILES]\n- D:\\a.py\n- D:\\b.py\n"
            "[LAST_ERROR]\n- Fatal error\n"
        )
        sections = parse_sections(text)
        bounded = bound_sections(sections, max_chars=700)
        self.assertLessEqual(len(bounded), 700)
        self.assertIn("[GOAL]", bounded)
        self.assertIn("[NEXT]", bounded)
        self.assertIn("[LAST_ERROR]", bounded)
        self.assertIn("[FILES]", bounded)

    def test_ledger_pointers_only(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = FactLedger(td)
            ledger.note_user_goal("Refactor parser module")
            ledger.note_tool_calls(
                [
                    {
                        "id": "tc_1",
                        "function": {
                            "name": "Write",
                            "arguments": json.dumps(
                                {
                                    "file_path": "D:\\code\\test.py",
                                    "content": "SECRET_LARGE_CONTENT_" * 100,
                                }
                            ),
                        },
                    }
                ]
            )
            ledger.note_tool_result("tc_1", "Successfully saved 5000 bytes")

            # Check that content is never stored in ledger
            raw_json = json.dumps(ledger.data)
            self.assertNotIn("SECRET_LARGE_CONTENT_", raw_json)
            self.assertIn("D:\\code\\test.py", ledger.data["files"])

            # Persist and reload
            ledger2 = FactLedger(td)
            self.assertEqual(ledger2.data["goal"], "Refactor parser module")
            self.assertIn("D:\\code\\test.py", ledger2.data["files"])

    def test_handoff_message_size(self):
        bounded = (
            "[GOAL]\nBuild API\n[DONE]\n- Task 1\n[NEXT]\n- Task 2\n"
            "[FILES]\n- D:\\a.py\n[LAST_ERROR]\nNone\n"
        )
        recent = [
            f"user: message {i} with long description and arguments"
            for i in range(6)
        ]
        msg = build_handoff_message(bounded, recent, "D:\\handoff\\latest.md", 2000)
        self.assertLessEqual(len(msg), 2000)
        self.assertIn("=== HANDOFF", msg)
        self.assertIn("=== END HANDOFF ===", msg)
        self.assertIn("D:\\handoff\\latest.md", msg)

    def test_chain_overlap(self):
        with tempfile.TemporaryDirectory() as td:
            f1 = Path(td) / ".bridge_chain_acc1.json"
            f2 = Path(td) / ".bridge_chain_acc2.json"
            write_chain_file(f1, "acc1", 10000, ["keyA", "keyB"], 1000.0)
            write_chain_file(f2, "acc2", 10001, ["keyB", "keyC"], 1000.0)

            overlap = find_chain_overlap(td, f1, ["keyA", "keyB"])
            self.assertEqual(len(overlap), 1)
            self.assertEqual(overlap[0]["key"], "keyB")
            self.assertEqual(overlap[0]["other_instance"], "acc2")

            # No overlap for non-shared keys
            f3 = Path(td) / ".bridge_chain_acc3.json"
            write_chain_file(f3, "acc3", 10002, ["keyZ"], 1000.0)
            self.assertEqual(find_chain_overlap(td, f3, ["keyZ"]), [])


class TestMockQuotaSwitch(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.td.name)
        self.handoff_dir = self.base_dir / "handoff" / "test"
        self.state_file = self.base_dir / ".bridge_accounts.json"

    def tearDown(self):
        self.td.cleanup()

    def _make_manager(self, num_accounts=2):
        m = gs.SingleChatManager()
        m.state = "on"
        m.chain = [
            {
                "label": f"acc_{i}",
                "psid": f"psid_{i}",
                "psidts": f"ts_{i}",
                "key": f"key_{i:012d}",
            }
            for i in range(num_accounts)
        ]
        m.active_index = 0
        m._active_psid = m.chain[0]["psid"]
        m.account_state = {}
        m.auth_ok = True
        m.session_initialized = True
        m.tool_header_sent = True
        m.available_models = ["models/gemini-2.5-pro", "models/gemini-2.5-flash"]
        m.ledger = FactLedger(self.handoff_dir)

        # Mock client to prevent any real network requests
        mock_client = SimpleNamespace(
            account_status=AccountStatus.AVAILABLE,
            _cookie_source="Cache",
            init=AsyncMock(),
            close=AsyncMock(),
            list_models=MagicMock(
                return_value=["models/gemini-2.5-pro", "models/gemini-2.5-flash"]
            ),
            start_chat=MagicMock(),
        )
        m.client = mock_client
        m._evaluate_auth = MagicMock(return_value=True)
        return m

    async def test_quota_switches_account_and_retries(self):
        m = self._make_manager(2)

        sent_prompts = []
        call_count = 0

        async def fake_send_message(prompt, files=None):
            nonlocal call_count
            call_count += 1
            sent_prompts.append(prompt)
            if call_count == 1:
                raise UsageLimitExceededError("Quota exhausted on acc_0")
            return SimpleNamespace(text="Success response from acc_1")

        mock_chat = SimpleNamespace(send_message=fake_send_message, metadata=["cid_1", "rid_1"])
        m.chat_session = mock_chat
        m.client.start_chat = MagicMock(return_value=mock_chat)

        async def fake_switch(idx):
            m.active_index = idx
            m._active_psid = m.chain[idx]["psid"]
            m.chat_session = mock_chat
            return True

        m._switch_to_account = fake_switch
        m._ask_flash_for_handoff = AsyncMock(return_value="[GOAL]\nBuild app\n[DONE]\n- Step 1\n[NEXT]\n- Step 2\n")

        with patch.dict(gs.INSTANCE_PATHS, {"accounts_state_file": self.state_file, "handoff_dir": self.handoff_dir}):
            reply = await m.send_prepared_turn("Please write the code", [])

        self.assertEqual(reply, "Success response from acc_1")
        self.assertEqual(m.active_index, 1)
        self.assertIn("=== HANDOFF", sent_prompts[1])
        self.assertIn("Please write the code", sent_prompts[1])
        # Old account marked resting
        self.assertIn(m.chain[0]["key"], m.account_state)

    async def test_flash_failure_falls_back_to_ledger(self):
        m = self._make_manager(2)
        m.ledger.note_user_goal("Emergency fix")
        m.ledger.note_tool_calls([
            {"id": "c1", "function": {"name": "Grep", "arguments": json.dumps({"pattern": "bug"})}}
        ])

        call_count = 0
        sent_prompts = []

        async def fake_send_message(prompt, files=None):
            nonlocal call_count
            call_count += 1
            sent_prompts.append(prompt)
            if call_count == 1:
                raise UsageLimitExceededError("Quota reached")
            return SimpleNamespace(text="Done from acc_1")

        mock_chat = SimpleNamespace(send_message=fake_send_message, metadata=["cid_1", "rid_1"])
        m.chat_session = mock_chat
        m.client.start_chat = MagicMock(return_value=mock_chat)

        async def fake_switch(idx):
            m.active_index = idx
            m._active_psid = m.chain[idx]["psid"]
            m.chat_session = mock_chat
            return True

        m._switch_to_account = fake_switch
        m._ask_flash_for_handoff = AsyncMock(side_effect=RuntimeError("Flash error"))

        with patch.dict(gs.INSTANCE_PATHS, {"accounts_state_file": self.state_file, "handoff_dir": self.handoff_dir}):
            reply = await m.send_prepared_turn("Continue task", [])

        self.assertEqual(reply, "Done from acc_1")
        self.assertEqual(m.active_index, 1)
        self.assertIn("Emergency fix", sent_prompts[1])

    async def test_all_exhausted_raises_quota_error(self):
        m = self._make_manager(1)  # Only 1 account

        async def fake_send_message(prompt, files=None):
            raise UsageLimitExceededError("Quota exhausted on sole account")

        m.chat_session = SimpleNamespace(send_message=fake_send_message)

        with patch.dict(gs.INSTANCE_PATHS, {"accounts_state_file": self.state_file, "handoff_dir": self.handoff_dir}):
            with self.assertRaises(BridgeQuotaError):
                await m.send_prepared_turn("Do work", [])

    async def test_stream_no_retry_after_partial_output(self):
        m = self._make_manager(2)

        async def fake_send_message_stream(prompt, files=None):
            yield SimpleNamespace(text_delta="Partial text delivered to user")
            raise UsageLimitExceededError("Quota exhausted after delta")

        m.chat_session = SimpleNamespace(send_message_stream=fake_send_message_stream)

        items = []
        with patch.dict(gs.INSTANCE_PATHS, {"accounts_state_file": self.state_file, "handoff_dir": self.handoff_dir}):
            async for item in m.send_prepared_turn_stream("Stream prompt", []):
                items.append(item)

        kinds = [it[0] for it in items]
        self.assertIn("delta", kinds)
        self.assertIn("error", kinds)
        # Verify active_index did NOT switch
        self.assertEqual(m.active_index, 0)

    async def test_handoff_not_resent_on_second_request(self):
        m = self._make_manager(2)
        sent_prompts = []
        call_count = 0

        async def fake_send_message(prompt, files=None):
            nonlocal call_count
            call_count += 1
            sent_prompts.append(prompt)
            if call_count == 1:
                raise UsageLimitExceededError("First account exhausted")
            return SimpleNamespace(text="OK")

        mock_chat = SimpleNamespace(send_message=fake_send_message, metadata=["cid_1", "rid_1"])
        m.chat_session = mock_chat
        m.client.start_chat = MagicMock(return_value=mock_chat)

        async def fake_switch(idx):
            m.active_index = idx
            m._active_psid = m.chain[idx]["psid"]
            m.chat_session = mock_chat
            return True

        m._switch_to_account = fake_switch
        m._ask_flash_for_handoff = AsyncMock(return_value="[GOAL]\nTask\n[DONE]\n- Done 1\n[NEXT]\n- Next 1\n")

        with patch.dict(gs.INSTANCE_PATHS, {"accounts_state_file": self.state_file, "handoff_dir": self.handoff_dir}):
            # Turn 1 triggers quota failover and sends handoff
            await m.send_prepared_turn("Turn 1 prompt", [])
            # Turn 2 on new account
            await m.send_prepared_turn("Turn 2 prompt", [])

        self.assertIn("=== HANDOFF", sent_prompts[1])
        self.assertNotIn("=== HANDOFF", sent_prompts[2])

    async def test_handoff_survives_transient_retry(self):
        m = self._make_manager(2)
        sent_prompts = []
        call_count = 0

        async def fake_send_message(prompt, files=None):
            nonlocal call_count
            call_count += 1
            sent_prompts.append(prompt)
            if call_count == 1:
                raise UsageLimitExceededError("Account 0 quota limit")
            elif call_count == 2:
                # Network glitch on account 1
                raise RuntimeError("Temporary connection reset")
            return SimpleNamespace(text="Finished after transient retry")

        mock_chat = SimpleNamespace(send_message=fake_send_message, metadata=["cid_1", "rid_1"])
        m.chat_session = mock_chat
        m.client.start_chat = MagicMock(return_value=mock_chat)

        async def fake_switch(idx):
            m.active_index = idx
            m._active_psid = m.chain[idx]["psid"]
            m.chat_session = mock_chat
            return True

        m._switch_to_account = fake_switch
        m._ask_flash_for_handoff = AsyncMock(return_value="[GOAL]\nSurvive retry\n[DONE]\n- D\n[NEXT]\n- N\n")

        with patch.dict(gs.INSTANCE_PATHS, {"accounts_state_file": self.state_file, "handoff_dir": self.handoff_dir}):
            reply = await m.send_prepared_turn("Transient retry test", [])

        self.assertEqual(reply, "Finished after transient retry")
        # Call 1: failed with quota
        # Call 2: failed with network glitch, contains HANDOFF
        # Call 3: retried attempt on account 1, MUST STILL contain HANDOFF
        self.assertIn("=== HANDOFF", sent_prompts[1])
        self.assertIn("=== HANDOFF", sent_prompts[2])

    async def test_failover_all_fail_restores_state(self):
        m = self._make_manager(3)
        initial_index = 0

        # Accounts 1 and 2 both fail auth when switching
        async def fake_switch_fail(idx):
            m.mark_exhausted_for(idx, "auth", 1.0)
            return False

        m._switch_to_account = fake_switch_fail
        m._rebuild_client = AsyncMock()

        success = await m._try_failover_to_backup()
        self.assertFalse(success)
        # Verified rebuild called with initial index
        m._rebuild_client.assert_awaited_once_with(initial_index)


if __name__ == "__main__":
    unittest.main()
