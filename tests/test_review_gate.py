"""ADR-037: review gate, evidence validation, and dynamic step budget."""

import json
import tempfile
import unittest
from pathlib import Path

from agent.core.harness import (
    Harness,
    HarnessPolicy,
    _PLAN_MAX_TOKENS,
    _PLAN_SCHEMA,
    _PLANNER_PROMPT,
    _constrained_answer,
)
from agent.core.learning import Review, Verdict
from agent.core.policy import Guardrail, GuardrailPolicy
from agent.core.protocol import ToolResult, ToolSpec
from agent.core.reflection import Reflection
from agent.core.report import Reporter
from agent.ports import BrainError, BrainReply


class FakeBrain:
    def __init__(self) -> None:
        self.plan_requests = []
        self.plan_max_tokens = []
        self.plan_calls = 0
        self.answer_calls = 0

    def complete(self, request):
        required = set((request.json_schema or {}).get("required", []))
        if "steps" in required:
            self.plan_calls += 1
            self.plan_requests.append(request.messages)
            self.plan_max_tokens.append(request.max_tokens)
            tools = 1 if self.plan_calls == 1 else 2
            steps = [
                {"kind": "tool", "target": "workspace.echo", "args": {}, "why": "do work"}
                for _ in range(tools)
            ]
            steps.append({"kind": "answer", "target": "", "args": {}, "why": "answer"})
            content = {"goal": "finish task", "rationale": "test gate", "steps": steps}
            return BrainReply(content=content, raw=json.dumps(content), tokens=1, elapsed_s=0.0)
        if "answer" in required:
            self.answer_calls += 1
            content = {"answer": "All tests passed.", "confidence": 1.0}
            return BrainReply(content=content, raw=json.dumps(content), tokens=1, elapsed_s=0.0)
        raise AssertionError(f"unexpected schema: {required}")

    def health(self) -> bool:
        return True


class FailingReplanBrain(FakeBrain):
    """First plan succeeds; the replan call fails as if JSON was truncated."""

    def complete(self, request):
        required = set((request.json_schema or {}).get("required", []))
        if "steps" in required and self.plan_calls >= 1:
            self.plan_calls += 1
            raise BrainError("model content is not JSON: truncated")
        return super().complete(request)


class FakeReviewer:
    def __init__(self) -> None:
        self.calls = 0

    def review(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return Review(
                verdict=Verdict.REVISE,
                score=0.0,
                issues=("first attempt is incomplete",),
                suggestions=("run the test and fix it",),
            )
        return Review(verdict=Verdict.PASS, score=1.0, issues=(), suggestions=())


class FakeTool:
    spec = ToolSpec(
        name="workspace.echo",
        category="workspace",
        description="fake tool",
        args_schema={"type": "object", "properties": {}},
    )

    def __init__(self) -> None:
        self.calls = 0

    def invoke(self, call):
        self.calls += 1
        return ToolResult(
            call_id=call.call_id,
            ok=True,
            data={
                "returncode": 1 if self.calls == 1 else 0,
                "argv": ["python3", "-m", "unittest", "test_demo.py"],
                "stdout": "FAILED" if self.calls == 1 else "OK",
            },
            elapsed_s=0.0,
        )


class FakeUI:
    def __init__(self) -> None:
        self.events = []
        self.reported = None

    def render(self, envelope) -> None:
        self.events.append(envelope)

    def report(self, envelope, ledger_path) -> None:
        self.reported = (envelope, ledger_path)

    def ask_consent(self, question: str) -> bool:
        return False


class FakeMemory:
    def recent(self, limit: int):
        return []

    def append(self, payload) -> None:
        pass


class FakeReflector:
    def reflect(self, *, turn_id, **kwargs):
        return Reflection(
            turn_id=turn_id,
            verdict="REVISE",
            lessons=("verify before claiming success",),
            plan_hint="run tests",
            tool_notes=(),
            created_at=0.0,
        )


class ReviewGateTest(unittest.TestCase):
    def test_revise_triggers_replan_with_feedback_and_dynamic_steps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brain = FakeBrain()
            reviewer = FakeReviewer()
            tool = FakeTool()
            from agent.tools.registry import ToolRegistry

            harness = Harness(
                brain=brain,
                tools=ToolRegistry([tool]),
                ui=FakeUI(),
                policy=HarnessPolicy(
                    max_steps=1,
                    temperature=0.0,
                    max_tokens=64,
                    max_review_repairs=1,
                    repair_step_bonus=2,
                ),
                guardrail=Guardrail(GuardrailPolicy(root=root, deny_names=())),
                reviewer=reviewer,
                reporter=Reporter(),
                reflector=FakeReflector(),
                memory=FakeMemory(),
                recall_limit=0,
                ledger_dir=root,
            )
            outcome = harness.run_turn("finish the benchmark")
            ledger_text = outcome.ledger_path.read_text()

        self.assertEqual(brain.plan_calls, 2)
        self.assertEqual(brain.plan_max_tokens, [64, 64])
        self.assertEqual(reviewer.calls, 2)
        self.assertEqual(tool.calls, 3)
        self.assertIsNotNone(outcome.review)
        assert outcome.review is not None
        self.assertEqual(outcome.review.verdict, Verdict.PASS)
        self.assertIn("first attempt is incomplete", brain.plan_requests[1][-1]["content"])

        events = [json.loads(line) for line in ledger_text.splitlines()]
        kinds = [event["kind"] for event in events]
        self.assertEqual(kinds.count("PLAN"), 2)
        self.assertIn("RETRY", kinds)
        self.assertEqual(kinds.count("ANSWER"), 1)
        reviews = [event for event in events if event["kind"] == "REVIEW"]
        self.assertTrue(any(event.get("meta", {}).get("gate") == "evidence" for event in reviews))
        self.assertTrue(any("answer claims" in issue for event in reviews for issue in event["payload"]["issues"]))

    def test_language_and_plan_bounds(self) -> None:
        self.assertIn("answer in that language", _PLANNER_PROMPT)
        self.assertIn("also plan one English query", _PLANNER_PROMPT)
        self.assertEqual(_PLAN_SCHEMA["properties"]["steps"]["maxItems"], 12)
        self.assertLessEqual(_PLAN_MAX_TOKENS, 2048)

    def test_replan_failure_returns_constrained_answer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            brain = FailingReplanBrain()
            reviewer = FakeReviewer()
            tool = FakeTool()
            from agent.tools.registry import ToolRegistry

            harness = Harness(
                brain=brain,
                tools=ToolRegistry([tool]),
                ui=FakeUI(),
                policy=HarnessPolicy(
                    max_steps=1,
                    temperature=0.0,
                    max_tokens=64,
                    max_review_repairs=1,
                    repair_step_bonus=2,
                ),
                guardrail=Guardrail(GuardrailPolicy(root=root, deny_names=())),
                reviewer=reviewer,
                reporter=Reporter(),
                reflector=FakeReflector(),
                memory=FakeMemory(),
                recall_limit=0,
                ledger_dir=root,
            )
            outcome = harness.run_turn("完成基准测试")
            ledger_text = outcome.ledger_path.read_text()

        self.assertEqual(brain.plan_calls, 2)
        self.assertEqual(reviewer.calls, 1)
        self.assertEqual(tool.calls, 1)
        assert outcome.review is not None
        self.assertEqual(outcome.review.verdict, Verdict.REVISE)
        text = outcome.answer.payload["text"]
        self.assertIn("All tests passed.", text)
        self.assertIn("未完成说明", text)
        kinds = [json.loads(line)["kind"] for line in ledger_text.splitlines()]
        self.assertIn("ERROR", kinds)
        self.assertIn("ANSWER", kinds)


if __name__ == "__main__":
    unittest.main()
