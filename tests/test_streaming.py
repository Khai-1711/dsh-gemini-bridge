import unittest

from app.streaming import ToolCallAwareStreamer


def _run(deltas):
    s = ToolCallAwareStreamer()
    pieces = []
    for d in deltas:
        pieces.extend(s.feed(d))
    pieces.extend(s.flush())
    return "".join(pieces), s.full_text


class TestToolCallAwareStreamer(unittest.TestCase):
    def test_plain_text_passthrough(self):
        out, full = _run(["hello ", "world"])
        self.assertEqual(out, "hello world")
        self.assertEqual(full, "hello world")

    def test_tool_call_withheld(self):
        text = 'a\n<tool_call>{"name":"x"}</tool_call>\nb'
        out, full = _run([text])
        self.assertEqual(full, text)
        self.assertEqual(out, "a\n\nb")

    def test_partial_tag_across_chunks(self):
        deltas = ["hi <too", "l_call>xx</too", "l_call> bye"]
        out, full = _run(deltas)
        self.assertEqual(full, "hi <tool_call>xx</tool_call> bye")
        self.assertEqual(out, "hi  bye")

    def test_multiple_tool_calls(self):
        text = "<tool_call>a</tool_call>mid<tool_call>b</tool_call>end"
        out, full = _run([text])
        self.assertEqual(full, text)
        self.assertEqual(out, "midend")

    def test_unclosed_tool_call_withheld(self):
        text = "start <tool_call>never closed"
        out, full = _run([text])
        self.assertEqual(full, text)
        self.assertEqual(out, "start ")

    def test_text_before_and_after(self):
        deltas = ["before ", "<tool_call>json</tool_call>", " after"]
        out, full = _run(deltas)
        self.assertEqual(full, "before <tool_call>json</tool_call> after")
        self.assertEqual(out, "before  after")

    def test_inline_backtick_tool_call_in_prose_passes_through_without_swallowing(self):
        deltas = [
            "Use plain-text `",
            "<tool_call>",
            "` (for example `pwsh`) to run tests.",
        ]
        out, full = _run(deltas)
        expected = "Use plain-text `<tool_call>` (for example `pwsh`) to run tests."
        self.assertEqual(full, expected)
        self.assertEqual(out, expected)


if __name__ == "__main__":
    unittest.main()
