import json
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

    def test_smart_truncate_only_middle_errors(self):
        text = "HEAD_CONTENT\n" + ("line ok\n" * 50) + "middle ERROR: fail here\n" + ("line ok\n" * 50) + "TAIL_CONTENT"
        out = smart_truncate(text, 200)
        self.assertIn("ERROR: fail here", out)
        self.assertIn("HEAD_CONTENT", out)
        self.assertIn("TAIL_CONTENT", out)

    def test_smart_truncate_tail_zero(self):
        text = "A" * 1000
        out = smart_truncate(text, 100)
        self.assertLessEqual(len(out), 120)


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

    def test_sanitize_command_allows_format_flag(self):
        for cmd in ["npx prettier --format src/", "black --format", "ruff format ."]:
            self.assertEqual(sanitize_shell_command_no_c_drive(cmd), cmd)

    def test_safe_path_relative_to_project_dir(self):
        self.assertTrue(is_safe_path_no_c_drive("src/main.py"))
        self.assertTrue(is_safe_path_no_c_drive(r"app\subsystem\logic.py"))

    def test_safe_path_blocks_c_drive_and_system(self):
        self.assertFalse(is_safe_path_no_c_drive(r"C:\Windows"))
        self.assertFalse(is_safe_path_no_c_drive(r"C:\Program Files\app"))
        self.assertFalse(is_safe_path_no_c_drive(r"..\..\Windows"))


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

    def test_parse_tool_call_with_parameters_at_top_level(self):
        reply = (
            '<tool_call>{"name":"create_goal","objective":"Build backend",'
            '"max_goal_rounds":5}</tool_call>'
        )
        tools = {
            "create_goal": {
                "parameters": {
                    "properties": {
                        "objective": {"type": "string"},
                        "max_goal_rounds": {"type": "integer"},
                    },
                    "required": ["objective"],
                }
            }
        }

        calls = parse_tool_calls_from_reply(reply, tools)

        self.assertEqual(len(calls), 1)
        self.assertEqual(
            json.loads(calls[0]["function"]["arguments"]),
            {"objective": "Build backend", "max_goal_rounds": 5},
        )

    def test_parse_tool_call_with_unescaped_windows_path(self):
        reply = (
            r'<tool_call>{"name":"Write","file_path":"D:\Users\me\main.py",'
            r'"content":"print(1)"}</tool_call>'
        )
        tools = {
            "Write": {
                "parameters": {
                    "properties": {
                        "file_path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["file_path", "content"],
                }
            }
        }

        calls = parse_tool_calls_from_reply(reply, tools)

        self.assertEqual(len(calls), 1)
        args = json.loads(calls[0]["function"]["arguments"])
        self.assertEqual(args["file_path"], r"D:\Users\me\main.py")
        self.assertEqual(args["content"], "print(1)")

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

    def test_normalize_tool_arguments_preserves_path_when_schema_accepts_path(self):
        schema = {"parameters": {"properties": {"path": {"type": "string"}}}}
        args = {"path": r"D:\test\file.txt"}
        out = normalize_tool_arguments("CustomTool", args, schema)
        self.assertIn("path", out)
        self.assertNotIn("file_path", out)

    def test_normalize_tool_arguments_no_fake_command(self):
        schema = {"parameters": {"properties": {"command": {"type": "string"}}}}
        args = {"arg1": "val1"}
        out = normalize_tool_arguments("Pwsh", args, schema)
        self.assertNotIn("command", out)

    def test_parse_tool_calls_with_empty_tools_map_accepts_tagged(self):
        reply = '<tool_call>{"name": "AnyTool", "arguments": {}}</tool_call>'
        calls = parse_tool_calls_from_reply(reply, {})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "AnyTool")

    def test_parse_tool_calls_prefix_stripped_correctly(self):
        reply = '<tool_call>{"name": "functions.MyTool", "arguments": {}}</tool_call>'
        calls = parse_tool_calls_from_reply(reply, {})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "MyTool")


if __name__ == "__main__":
    unittest.main()
