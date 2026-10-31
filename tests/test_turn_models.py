"""Per-turn model accounting for RunReport (ADR-034)."""

import tempfile
import unittest
from pathlib import Path

from agent.adapters.llama_cpp import ServerSupervisor
from agent.config import ModelProfile


def _profile(alias: str, port: int) -> ModelProfile:
    return ModelProfile(
        alias=alias,
        gguf=Path(f"/nonexistent/{alias}.gguf"),
        host="127.0.0.1",
        port=port,
        n_ctx=512,
        n_gpu_layers=16,
    )


class FakeSupervisor(ServerSupervisor):
    """Avoid starting a real llama-server while exercising ensure()."""

    def _acquire_lock(self) -> None:
        return None

    def _is_alive(self) -> bool:
        return True

    def _start(self, profile) -> None:
        self._ngl_used = 16


class PerTurnModelAccountingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.sup = FakeSupervisor(
            binary=Path("/nonexistent/llama-server"),
            profiles={
                "main": _profile("main", 18090),
                "sub": _profile("sub", 18081),
            },
            lock_path=Path(tempfile.gettempdir()) / "agent-test-model.lock",
            log_dir=Path(tempfile.gettempdir()) / "agent-test-logs",
            health_timeout_s=1.0,
        )

    def test_begin_turn_resets_previous_models(self) -> None:
        self.sup.begin_turn()
        self.sup.ensure("main")
        self.assertEqual(self.sup.snapshot(), {"main": 16})

        self.sup.begin_turn()
        self.sup.ensure("sub")
        self.assertEqual(self.sup.snapshot(), {"sub": 16})

    def test_reusing_resident_model_still_counts_as_used(self) -> None:
        self.sup.begin_turn()
        self.sup.ensure("sub")
        self.sup.ensure("sub")
        self.assertEqual(self.sup.snapshot(), {"sub": 16})

    def test_snapshot_is_empty_before_any_ensure(self) -> None:
        self.sup.begin_turn()
        self.assertEqual(self.sup.snapshot(), {})


if __name__ == "__main__":
    unittest.main()
