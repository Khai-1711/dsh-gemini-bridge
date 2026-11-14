import logging
import unittest

# Silence the bridge logger (avoids touching logs/bridge.log during tests).
logging.disable(logging.CRITICAL)
logging.raiseExceptions = False

from app.prompt_builder import build_incremental_prompt_and_files, smart_truncate
from app.security import is_safe_path_no_c_drive, sanitize_shell_command_no_c_drive
from app.tool_parser import normalize_tool_arguments, parse_tool_calls_from_reply


class TestSmartTruncate(unittest.TestCase):
    def test_short_text_unchanged(self):
        text = "hello world"
        self.assertEqual(smart_truncate(text, 100), text)

    def test_truncation_preserves_errors(self):
        text = ("line1 ok\n" * 50) + "ERROR: boom at line 99\n" + ("line2 ok\n" * 50)
        out = smart_truncate(text, 400)
        self.assertIn("ERROR: boom", out)
        self.assertLessEqual(len(out), 400)

    def test_many_long_error_lines_respect_limit(self):
        error_line = "ERROR " + "x" * 90
        text = "\n".join([error_line] * 30) + "\n" + ("ok\n" * 100)
        out = smart_truncate(text, 2000)
        self.assertLessEqual(len(out), 2000)


class TestPromptBuilder(unittest.TestCase):
    def test_first_turn_includes_tool_header(self):
        tools = [{"type": "function", "function": {"name": "Read", "description": "read a file"}}]
        messages = [{"role": "user", "content": "review code"}]
        prompt, _ = build_incremental_prompt_and_files(messages, tools, is_first_turn=True)
        self.assertIn("OPERATOR INSTRUCTIONS", prompt)
        self.assertIn("Read", prompt)

    def test_subsequent_turn_omits_tool_header(self):
        tools = [{"type": "function", "function": {"name": "Read", "description": "read a file"}}]
        messages = [{"role": "user", "content": "continue"}]
        prompt, _ = build_incremental_prompt_and_files(messages, tools, is_first_turn=False)
        self.assertNotIn("OPERATOR INSTRUCTIONS", prompt)

    def test_needs_tool_header_states(self):
        def calc_needs_tool_header(session_initialized: bool, tool_header_sent: bool) -> bool:
            return (not session_initialized) or (not tool_header_sent)

        # Fresh session: needs header
        self.assertTrue(calc_needs_tool_header(session_initialized=False, tool_header_sent=False))
        # Resumed session: initialized but header not yet sent in this process -> needs header
        self.assertTrue(calc_needs_tool_header(session_initialized=True, tool_header_sent=False))
        # Normal conversation flow after turn 1: do not need header
        self.assertFalse(calc_needs_tool_header(session_initialized=True, tool_header_sent=True))


class TestSecurity(unittest.TestCase):
    def test_block_c_drive(self):
        for cmd in [
            r"Get-Content C:\Windows\System32\x.txt",
            "$env:SystemDrive\\Windows",
            "Invoke-Expression 'hi'",
            "format c:",
        ]:
            out = sanitize_shell_command_no_c_drive(cmd)
            self.assertIn("SECURITY BLOCK", out, msg=cmd)

    def test_allow_d_drive(self):
        cmd = r"Get-ChildItem D:\ABC"
        self.assertEqual(sanitize_shell_command_no_c_drive(cmd), cmd)

    def test_path_safety(self):
        self.assertFalse(is_safe_path_no_c_drive(r"C:\Windows"))
        self.assertFalse(is_safe_path_no_c_drive(r"..\..\Windows\System32"))
        self.assertTrue(is_safe_path_no_c_drive(r"D:\ABC\file.txt"))


class TestToolParser(unittest.TestCase):
    def test_normalize_path_alias(self):
        args = {"path": r"D:\x\y.txt"}
        out = normalize_tool_arguments("Read", args, {"parameters": {"properties": {}}})
        self.assertIn("file_path", out)
        self.assertEqual(out["file_path"], r"D:\x\y.txt")

    def test_normalize_command_alias(self):
        args = {"cmd": "dir"}
        out = normalize_tool_arguments("Pwsh", args, {"parameters": {"properties": {}}})
        self.assertIn("command", out)
        self.assertEqual(out["command"], "dir")
        self.assertIn("description", out)

    def test_parse_single_tool_call(self):
        reply = '<tool_call>\n{"name": "Pwsh", "arguments": {"command": "dir"}}\n</tool_call>'
        calls = parse_tool_calls_from_reply(reply, {})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "Pwsh")

    def test_parse_multiple_tool_calls(self):
        reply = (
            '<tool_call>{"name":"A","arguments":{}}</tool_call>'
            '<tool_call>{"name":"B","arguments":{}}</tool_call>'
        )
        calls = parse_tool_calls_from_reply(reply, {})
        self.assertEqual(len(calls), 2)

    def test_forbidden_tool_filtered(self):
        reply = '<tool_call>{"name":"read_image","arguments":{}}</tool_call>'
        calls = parse_tool_calls_from_reply(reply, {})
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
