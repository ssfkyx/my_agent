"""ADR-041: Agents_opinion archive for main-model harness proposals."""

import tempfile
import unittest
from pathlib import Path

from agent.adapters.opinions import OpinionLog
from agent.core.policy import Guardrail, GuardrailPolicy
from agent.core.protocol import ToolCall, ToolSpec


class OpinionLogTest(unittest.TestCase):
    def test_records_v2_and_indexes_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = OpinionLog(Path(tmp))
            proposal = {
                "proposal_id": "p-123",
                "schema_version": 2,
                "target_kind": "prompt",
                "target": "agent/core/harness.py:_PLANNER_PROMPT",
                "current_behavior": "planner guesses doc_id",
                "root_cause": "plan args are fixed",
                "alternatives": [
                    {"option": "a", "description": "desc a", "tradeoff": "cost a"},
                    {"option": "b", "description": "desc b", "tradeoff": "cost b"},
                ],
                "proposal": "add a validation step",
                "expected_metric": {
                    "metric": "not_found",
                    "baseline": "3",
                    "target": "0",
                    "method": "replay",
                },
                "replay_plan": ["python3 tests/run_all.py"],
                "risk": "low",
                "rollback": "revert the prompt",
                "status": "proposed",
                "created_at": 0.0,
            }
            first = log.record(proposal)
            second = log.record(proposal)
            self.assertEqual(first, second)
            text = first.read_text()
            self.assertIn("planner guesses doc_id", text)
            self.assertIn("desc a", text)
            self.assertIn("not_found", text)
            self.assertIn("revert the prompt", text)
            index = (Path(tmp) / "INDEX.md").read_text()
            self.assertEqual(index.count("- `p-123`"), 1)

    def test_records_v1_proposal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = OpinionLog(Path(tmp))
            path = log.record(
                {
                    "proposal_id": "p-v1",
                    "target_kind": "prompt",
                    "target": "planner system prompt",
                    "rationale": "old style rationale",
                    "proposal": "old style proposal",
                    "expected_effect": "old style effect",
                    "tests": ["python3 tests/run_all.py"],
                    "risk": "low",
                    "created_at": 0.0,
                }
            )
            text = path.read_text()
            self.assertIn("old style rationale", text)
            self.assertIn("old style effect", text)


    def test_agents_opinion_is_denied_by_guardrail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Agents_opinion").mkdir()
            guardrail = Guardrail(GuardrailPolicy(root=root, deny_names=("Agents_opinion",)))
            outcome = guardrail.check_tool(
                ToolCall(
                    call_id="c-1",
                    tool="workspace.read_file",
                    args={"path": "Agents_opinion/INDEX.md"},
                ),
                ToolSpec(
                    name="workspace.read_file",
                    category="workspace",
                    description="read",
                    args_schema={"type": "object", "properties": {"path": {"type": "string"}}},
                    side_effect=False,
                ),
            )
            self.assertFalse(outcome.success)
            self.assertEqual(outcome.error.code, "DENIED_BY_GUARDRAIL")


if __name__ == "__main__":
    unittest.main()
