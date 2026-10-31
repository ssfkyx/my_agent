"""ADR-042: optional sub-brain and media models."""

import tempfile
import unittest
from pathlib import Path

from agent.__main__ import _parse_model_options
from agent.adapters.llama_cpp import RoleRegistry
from agent.config import MediaSettings
from agent.core.harness import Harness, HarnessPolicy
from agent.core.policy import Guardrail, GuardrailPolicy
from agent.core.report import Reporter
from agent.tools.perception import build_tools
from agent.tools.registry import ToolRegistry
from agent.ui.web import WebUI


class FakeUI:
    def render(self, envelope) -> None:
        pass

    def report(self, envelope, ledger_path) -> None:
        pass


class FakeMemory:
    def recent(self, limit):
        return []

    def append(self, payload) -> None:
        pass

    def record_tool_request(self, payload) -> None:
        pass


class OptionalModelsTest(unittest.TestCase):
    def test_parse_model_options(self) -> None:
        overrides, disabled = _parse_model_options(
            ["main=/models/main.gguf", "sub=none"],
            ["vl"],
        )
        self.assertEqual(overrides["main"], "/models/main.gguf")
        self.assertEqual(disabled, ("sub", "vl"))

    def test_media_settings_allows_empty_aliases(self) -> None:
        media = MediaSettings("", "", 1024, 1024)
        self.assertEqual(media.asr_alias, "")
        self.assertEqual(media.vl_alias, "")

    def test_perception_tools_are_partial(self) -> None:
        names = {tool.spec.name for tool in build_tools(object(), enable_asr=True, enable_vl=False)}
        self.assertIn("perception.attach_file", names)
        self.assertIn("perception.transcribe", names)
        self.assertNotIn("perception.describe_image", names)

    def test_harness_allows_missing_reviewer_and_reflector(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            roles = RoleRegistry({"main": "main"})
            self.assertEqual(roles.current, "main")
            harness = Harness(
                brain=object(),
                tools=ToolRegistry([]),
                ui=FakeUI(),
                policy=HarnessPolicy(max_steps=1, temperature=0.0, max_tokens=64),
                guardrail=Guardrail(GuardrailPolicy(root=root, deny_names=())),
                reviewer=None,
                reporter=Reporter(),
                reflector=None,
                memory=FakeMemory(),
                recall_limit=0,
                ledger_dir=root,
                roles=roles,
            )
            self.assertIsNone(harness._review(None, None, [], ""))
            self.assertIsNone(harness._reflect(None, None, [], "", None))


    def test_web_upload_disabled_without_media(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            upload_root = root / "artifacts"
            upload_root.mkdir()
            ui = WebUI(
                host="127.0.0.1",
                port=0,
                consent_mode="deny",
                upload_root=upload_root,
                workspace_root=root,
                media_enabled=False,
            )
            try:
                self.assertFalse(ui.upload_enabled)
                status, body = ui.save_upload("default", "probe.png", b"x")
                self.assertEqual(status, 403)
                self.assertFalse(body["ok"])
            finally:
                ui.close()


    def test_web_upload_gates_each_modality(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            upload_root = root / "artifacts"
            upload_root.mkdir()
            ui = WebUI(
                host="127.0.0.1",
                port=0,
                consent_mode="deny",
                upload_root=upload_root,
                workspace_root=root,
                image_enabled=False,
                audio_enabled=True,
            )
            try:
                status, _ = ui.save_upload("default", "probe.png", b"x")
                self.assertEqual(status, 403)
                status, _ = ui.save_upload("default", "probe.wav", b"x")
                self.assertEqual(status, 200)
            finally:
                ui.close()


if __name__ == "__main__":
    unittest.main()
