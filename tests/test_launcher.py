"""ADR-043: double-click launcher helpers."""

import tempfile
import unittest
from pathlib import Path

import start


class LauncherTest(unittest.TestCase):
    def test_default_mode_is_web(self) -> None:
        self.assertEqual(start._agent_args([]), ["--web"])
        self.assertEqual(start._agent_args(["--once", "hi"]), ["--once", "hi"])

    def test_web_url_reads_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "test.toml"
            config.write_text('[web]\nhost = "127.0.0.1"\nport = 9123\n', encoding="utf-8")
            self.assertEqual(
                start._web_url(["--config", str(config)]),
                "http://127.0.0.1:9123/",
            )


if __name__ == "__main__":
    unittest.main()
