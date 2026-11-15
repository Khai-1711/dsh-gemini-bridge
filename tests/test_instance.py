import json
import logging
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

os.environ.pop("BRIDGE_INSTANCE", None)
logging.disable(logging.NOTSET)

from app.activity import ActivityJournal, summarize_tool_calls
from app.config import build_instance_paths, sanitize_instance_id
import app.gemini_session as gs


class TestInstanceIsolation(unittest.TestCase):
    def test_build_instance_paths_legacy(self):
        base = Path("X")
        paths = build_instance_paths(base, "")
        self.assertEqual(paths["env_file"], base / ".env")
        self.assertEqual(paths["session_file"], base / ".bridge_session.json")
        self.assertEqual(paths["log_file"], base / "logs" / "bridge.log")
        self.assertEqual(paths["activity_file"], base / "logs" / "activity.jsonl")
        self.assertEqual(paths["cookie_dir"], base / ".gemini_cookies")
        self.assertEqual(paths["accounts_state_file"], base / ".bridge_accounts.json")
        self.assertEqual(paths["chain_file"], base / ".bridge_chain.json")
        self.assertEqual(paths["handoff_dir"], base / "handoff" / "default")

    def test_build_instance_paths_acc2(self):
        base = Path("X")
        paths = build_instance_paths(base, "acc2")
        self.assertEqual(paths["env_file"], base / ".env.acc2")
        self.assertEqual(paths["session_file"], base / ".bridge_session_acc2.json")
        self.assertEqual(paths["log_file"], base / "logs" / "bridge_acc2.log")
        self.assertEqual(paths["activity_file"], base / "logs" / "activity_acc2.jsonl")
        self.assertEqual(paths["cookie_dir"], base / ".gemini_cookies_acc2")
        self.assertEqual(paths["accounts_state_file"], base / ".bridge_accounts_acc2.json")
        self.assertEqual(paths["chain_file"], base / ".bridge_chain_acc2.json")
        self.assertEqual(paths["handoff_dir"], base / "handoff" / "acc2")

    def test_two_ids_never_share_a_path(self):
        base = Path("X")
        paths1 = build_instance_paths(base, "acc1")
        paths2 = build_instance_paths(base, "acc2")
        vals1 = set(paths1.values())
        vals2 = set(paths2.values())
        self.assertTrue(vals1.isdisjoint(vals2))

    def test_sanitize_instance_id(self):
        self.assertEqual(sanitize_instance_id(""), "")
        self.assertEqual(sanitize_instance_id(None), "")
        self.assertEqual(sanitize_instance_id("acc2"), "acc2")
        self.assertEqual(sanitize_instance_id("acc_1-prod"), "acc_1-prod")
        for bad in ["a/b", "..", "x" * 17, "a b", "acc@1", "acc.2"]:
            with self.assertRaises(ValueError):
                sanitize_instance_id(bad)

    def test_activity_journals_isolated(self):
        prev_disable = logging.root.manager.disable
        logging.disable(logging.NOTSET)
        try:
            with tempfile.TemporaryDirectory() as td:
                f1 = Path(td) / "activity_acc1.jsonl"
                f2 = Path(td) / "activity_acc2.jsonl"
                j1 = ActivityJournal(f1, "acc1")
                j2 = ActivityJournal(f2, "acc2")
                try:
                    e1 = j1.write(task="task 1 for acc1")
                    e2 = j1.write(task="task 2 for acc1")
                    e3 = j2.write(task="task 1 for acc2")

                    self.assertEqual(e1["seq"], 1)
                    self.assertEqual(e2["seq"], 2)
                    self.assertEqual(e3["seq"], 1)

                    t1 = j1.tail(50)
                    t2 = j2.tail(50)
                    self.assertEqual(len(t1), 2)
                    self.assertEqual(len(t2), 1)
                    self.assertEqual(t1[0]["instance"], "acc1")
                    self.assertEqual(t1[1]["seq"], 2)
                    self.assertEqual(t2[0]["instance"], "acc2")

                    # Verify valid JSON per line
                    lines1 = f1.read_text(encoding="utf-8").strip().splitlines()
                    self.assertEqual(len(lines1), 2)
                    for l in lines1:
                        parsed = json.loads(l)
                        self.assertIn("ts", parsed)
                        self.assertEqual(parsed["instance"], "acc1")
                finally:
                    j1.close()
                    j2.close()
        finally:
            logging.disable(prev_disable)

    def test_summarize_tool_calls_no_content(self):
        calls = [
            {
                "id": "call_1",
                "function": {
                    "name": "Write",
                    "arguments": json.dumps(
                        {
                            "file_path": "D:\\project\\main.py",
                            "content": "SECRET_SUPER_LONG_CONTENT_" * 100,
                        }
                    ),
                },
            },
            {
                "id": "call_2",
                "function": {
                    "name": "Pwsh",
                    "arguments": json.dumps({"command": "dir /b D:\\project"}),
                },
            },
        ]
        summary = summarize_tool_calls(calls)
        self.assertEqual(len(summary), 2)
        self.assertEqual(summary[0]["name"], "Write")
        self.assertEqual(summary[0]["hint"], "D:\\project\\main.py")
        self.assertNotIn("SECRET_SUPER_LONG_CONTENT_", json.dumps(summary))
        self.assertEqual(summary[1]["name"], "Pwsh")
        self.assertEqual(summary[1]["hint"], "dir /b D:\\project")

    def test_current_chat_key(self):
        m = gs.SingleChatManager()
        # Initial or no cid -> "new"
        m.chat_session = None
        self.assertEqual(m.current_chat_key(), "new")

        m.chat_session = SimpleNamespace(metadata=["c_test_123", "r1", "rc1"])
        key = m.current_chat_key()
        self.assertEqual(len(key), 8)
        self.assertNotEqual(key, "new")

        # Same cid gives same key
        m2 = SimpleNamespace(metadata=["c_test_123", "r2", "rc2"])
        m.chat_session = m2
        self.assertEqual(m.current_chat_key(), key)

        # Different cid gives different key
        m.chat_session = SimpleNamespace(metadata=["c_other_999", "r1", "rc1"])
        self.assertNotEqual(m.current_chat_key(), key)


if __name__ == "__main__":
    unittest.main()
