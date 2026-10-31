"""Web upload + attachment hint tests (no model calls)."""

import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from agent.ui.web import WebUI


class WebUploadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.upload_root = self.root / "artifacts"
        self.upload_root.mkdir()
        self.ui = WebUI(
            host="127.0.0.1",
            port=0,
            consent_mode="deny",
            upload_root=self.upload_root,
            workspace_root=self.root,
            max_image_bytes=16,
            max_audio_bytes=16,
        )
        self.addCleanup(self.ui.close)

    def _post(self, path: str, data: bytes, content_type: str) -> tuple[int, dict]:
        request = urllib.request.Request(
            self.ui.url.rstrip("/") + path,
            data=data,
            headers={"Content-Type": content_type},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def _upload(self, filename: str, payload: bytes) -> tuple[int, dict]:
        boundary = "----agent-test-boundary"
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode("utf-8") + payload + f"\r\n--{boundary}--\r\n".encode("utf-8")
        return self._post("/upload", body, f"multipart/form-data; boundary={boundary}")

    def test_upload_saves_under_session_and_message_contains_hint(self) -> None:
        status, result = self._upload("probe.png", b"\x89PNG\r\n\x1a\n")
        self.assertEqual(status, 200, result)
        item = result["files"][0]
        self.assertEqual(item["kind"], "image")
        self.assertTrue(item["path"].startswith("artifacts/uploads/default/"))
        self.assertTrue((self.root / item["path"]).is_file())

        body = json.dumps({"text": "Describe this image", "attachments": [item["path"]]}).encode()
        status, result = self._post("/message", body, "application/json")
        self.assertEqual(status, 200, result)
        job = self.ui.next_job()
        self.assertIsNotNone(job)
        assert job is not None
        sid, text = job
        self.assertEqual(sid, "default")
        self.assertIn("[attachments]", text)
        self.assertIn(item["path"], text)
        self.assertIn("perception.describe_image", text)

    def test_rejects_unsupported_suffix(self) -> None:
        status, result = self._upload("notes.txt", b"hello")
        self.assertEqual(status, 400)
        self.assertIn("unsupported", result["error"])

    def test_rejects_oversized_file(self) -> None:
        status, result = self._upload("probe.png", b"x" * 17)
        self.assertEqual(status, 413)
        self.assertIn("exceeds limit", result["error"])

    def test_rejects_attachment_outside_session_upload_dir(self) -> None:
        outside = self.root / "docs"
        outside.mkdir()
        (outside / "secret.png").write_bytes(b"png")
        body = json.dumps({"text": "look", "attachments": ["docs/secret.png"]}).encode()
        status, result = self._post("/message", body, "application/json")
        self.assertEqual(status, 400)
        self.assertIn("invalid/empty", result["error"])


if __name__ == "__main__":
    unittest.main()
