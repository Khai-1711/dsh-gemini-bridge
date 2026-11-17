import json
import unittest

from app.prompt_builder import build_incremental_prompt_and_files
from app.title_generation import extract_local_title_request


class TestLocalTitleGeneration(unittest.TestCase):
    prefix = "Generate the session title from this JSON array of human messages:"

    def test_extracts_first_human_message_as_title(self):
        messages = [
            {
                "role": "user",
                "content": (
                    self.prefix
                    + '\n[{"seq":12,"text":"chào"}]'
                ),
            }
        ]

        recognized, title, _ = extract_local_title_request(messages)

        self.assertTrue(recognized)
        self.assertEqual(title, "chào")

    def test_title_is_whitespace_normalized_and_bounded(self):
        text = "  ".join(["mở đầu"] * 20)
        messages = [
            {
                "role": "user",
                "content": self.prefix + "\n" + json.dumps(
                    [{"seq": 1, "text": text}],
                    ensure_ascii=False,
                ),
            }
        ]

        recognized, title, _ = extract_local_title_request(messages)

        self.assertTrue(recognized)
        self.assertLessEqual(len(title), 83)
        self.assertTrue(title.endswith("..."))

    def test_non_title_request_is_not_intercepted(self):
        messages = [{"role": "user", "content": "chào"}]

        recognized, title, request_chars = extract_local_title_request(messages)

        self.assertFalse(recognized)
        self.assertIsNone(title)
        self.assertEqual(request_chars, 0)

    def test_normal_long_user_message_keeps_language_note(self):
        prompt, _ = build_incremental_prompt_and_files(
            [
                {
                    "role": "user",
                    "content": "Hãy giải thích thật chi tiết cách cấu hình dự án này.",
                }
            ],
            [],
            is_first_turn=False,
        )

        self.assertIn(
            "[NOTE: Reply in the EXACT SAME LANGUAGE as the USER REQUEST above.",
            prompt,
        )

    def test_malformed_title_request_is_recognized_but_rejected(self):
        messages = [
            {
                "role": "user",
                "content": self.prefix + "\n[{broken json]",
            }
        ]

        recognized, title, _ = extract_local_title_request(messages)

        self.assertTrue(recognized)
        self.assertIsNone(title)

    def test_rejects_trailing_text_and_non_human_entries(self):
        trailing_text = [
            {
                "role": "user",
                "content": self.prefix + '\n[{"seq":1,"text":"chào"}]\nextra',
            }
        ]
        assistant_entry = [
            {
                "role": "user",
                "content": (
                    self.prefix
                    + '\n[{"role":"assistant","seq":1,"text":"chào"}]'
                ),
            }
        ]

        self.assertEqual(
            extract_local_title_request(trailing_text)[:2],
            (True, None),
        )
        self.assertEqual(
            extract_local_title_request(assistant_entry)[:2],
            (True, None),
        )


if __name__ == "__main__":
    unittest.main()
