"""ADR-039: Phase 0 evolution proposals and failure journal."""

import json
import tempfile
import unittest
from pathlib import Path

from agent.adapters.opinions import OpinionLog
from agent.core.evolution import (
    ChangeProposal,
    EvolutionError,
    EvolutionStore,
    append_failure,
    build_evolution_report,
)
from agent.core.harness import Harness, HarnessPolicy
from agent.core.policy import Guardrail, GuardrailPolicy
from agent.core.protocol import ToolCall
from agent.core.reflection import Reflection
from agent.core.report import Reporter
from agent.ports import BrainError
from agent.tools.evolution import ProposeChangeTool
from agent.tools.registry import ToolRegistry


class EvolutionPhase0Test(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.proposals = self.root / "proposals.jsonl"
        self.failures = self.root / "failures.jsonl"
        self.store = EvolutionStore(self.proposals)

    def _proposal_args(self) -> dict:
        return {
            "target_kind": "prompt",
            "target": "agent/core/harness.py:_PLANNER_PROMPT",
            "current_behavior": "Planner occasionally omits verification steps.",
            "root_cause": "The prompt does not make verification mandatory for code changes.",
            "alternatives": [
                {
                    "option": "prompt-only",
                    "description": "Add a mandatory verification rule.",
                    "tradeoff": "Low cost, but depends on model compliance.",
                },
                {
                    "option": "plan-validator",
                    "description": "Reject code-write plans without a verification step.",
                    "tradeoff": "Stronger, but requires a new validator.",
                },
            ],
            "proposal": "Add a mandatory verification rule for code changes.",
            "expected_metric": {
                "metric": "unsupported_completion_claims",
                "baseline": "unknown",
                "target": "0 in 20 replay runs",
                "method": "run the replay set and count claims without test evidence",
            },
            "replay_plan": ["python3 tests/run_all.py"],
            "risk": "low",
            "rollback": "Revert the prompt change.",
        }

    def test_proposal_tool_queues_without_applying(self) -> None:
        tool = ProposeChangeTool(store=self.store, opinions=OpinionLog(self.root / "Agents_opinion"))
        result = tool.invoke(
            ToolCall(
                call_id="c-1",
                tool="harness.propose_change",
                args=self._proposal_args(),
            )
        )
        self.assertTrue(result.ok)
        self.assertTrue(result.data["queued"])
        entries = self.store.load()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["status"], "proposed")
        self.assertEqual(entries[0]["schema_version"], 2)
        self.assertEqual(entries[0]["target_kind"], "prompt")
        self.assertEqual(len(entries[0]["alternatives"]), 2)
        self.assertEqual(entries[0]["expected_metric"]["target"], "0 in 20 replay runs")
        self.assertIn("proposal_id", entries[0])
        self.assertTrue(Path(result.data["opinion_path"]).exists())

    def test_proposal_rejects_missing_target_file(self) -> None:
        args = self._proposal_args()
        args["target"] = "src/agents/planner.py"
        tool = ProposeChangeTool(store=self.store, root=self.root)
        result = tool.invoke(ToolCall(call_id="c-missing", tool="harness.propose_change", args=args))
        self.assertFalse(result.ok)
        self.assertIn("does not exist", result.error.message)
        self.assertEqual(self.store.load(), [])

    def test_proposal_requires_two_alternatives(self) -> None:
        args = self._proposal_args()
        args["alternatives"] = args["alternatives"][:1]
        with self.assertRaises(EvolutionError):
            ChangeProposal.from_payload(args)

    def test_proposal_rejects_invalid_kind(self) -> None:
        args = self._proposal_args()
        args["target_kind"] = "system"
        with self.assertRaises(EvolutionError):
            ChangeProposal.from_payload(args)

    def test_failure_journal_and_report(self) -> None:
        append_failure(
            self.failures,
            {"turn_id": "t-1", "code": "PLAN_INVALID", "message": "bad plan", "user_text": "q"},
        )
        append_failure(
            self.failures,
            {"turn_id": "t-2", "code": "ABORTED", "message": "cancelled", "user_text": "q"},
        )
        report = build_evolution_report(proposals_path=self.proposals, failures_path=self.failures)
        self.assertEqual(report["failure_count"], 2)
        self.assertEqual(report["failures_by_code"]["PLAN_INVALID"], 1)
        self.assertEqual(report["failures_by_code"]["ABORTED"], 1)
        self.assertEqual(json.loads(json.dumps(report))["failure_count"], 2)

    def test_reflection_change_request_roundtrip(self) -> None:
        reflection = Reflection.from_payload(
            {
                "lessons": ["search first"],
                "plan_hint": "use two queries",
                "tool_notes": ["use knowledge.search"],
                "change_request": "increase retrieval top_k for process questions",
            },
            turn_id="t-1",
            verdict="PASS",
        )
        self.assertEqual(
            reflection.to_payload()["change_request"],
            "increase retrieval top_k for process questions",
        )


class FailingBrain:
    def complete(self, request):
        raise BrainError("planner unavailable")

    def health(self) -> bool:
        return False


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


class HarnessFailureTest(unittest.TestCase):
    def test_hard_failure_is_journaled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            harness = Harness(
                brain=FailingBrain(),
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
            )
            outcome = harness.run_turn("trigger a hard failure")
            failure_path = root / "evolution" / "failures.jsonl"
            self.assertTrue(failure_path.exists())
            entries = [json.loads(line) for line in failure_path.read_text().splitlines()]
            self.assertEqual(entries[-1]["turn_id"], outcome.ledger_path.stem)
            self.assertEqual(entries[-1]["code"], "PLAN_INVALID")
            self.assertEqual(str(outcome.state), "FAILED")


if __name__ == "__main__":
    unittest.main()
