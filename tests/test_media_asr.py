"""ASR response normalisation (Qwen3-ASR marker format)."""

import unittest

from agent.adapters.media import _normalise_asr_text


class AsrTextTest(unittest.TestCase):
    def test_strips_language_prefix(self) -> None:
        self.assertEqual(
            _normalise_asr_text("language English<asr_text>Hello world"),
            "Hello world",
        )

    def test_empty_transcript_is_allowed(self) -> None:
        self.assertEqual(_normalise_asr_text("language None<asr_text>"), "")

    def test_text_without_marker_is_kept(self) -> None:
        self.assertEqual(_normalise_asr_text("  Hello  "), "Hello")


if __name__ == "__main__":
    unittest.main()
