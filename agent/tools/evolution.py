"""evolution — Phase 1 optimizer 提案工具（只入队，不执行）。

Module: agent.tools.evolution
Responsibility: 暴露 `harness.propose_change`，把主脑的可验证实验提案写入队列。
Depends on: stdlib (time) + agent.core.protocol + agent.core.evolution
Depended by: app（注册）, core.harness（调用）
ADRs: ADR-039（Phase 0 观察层）, ADR-040（Phase 1 optimizer 协议）
"""

import time
from pathlib import Path

from agent.adapters.opinions import OpinionLog
from agent.core.evolution import ChangeProposal, EvolutionError, EvolutionStore
from agent.core.protocol import ErrorCode, ErrorInfo, ToolCall, ToolResult, ToolSpec


class ProposeChangeTool:
    """把一条可验证的 harness 变更实验写入队列；不修改任何代码或配置。"""

    spec = ToolSpec(
        name="harness.propose_change",
        category="harness",
        description=(
            "Queue a Phase 1 optimizer proposal for a concrete harness experiment. "
            "It must include current behavior, root cause, at least two alternatives with tradeoffs, "
            "the selected change, a measurable expected metric, a replay plan, risk, and rollback. "
            "This only writes to the evolution queue; it never applies changes during the turn."
        ),
        args_schema={
            "type": "object",
            "properties": {
                "target_kind": {
                    "type": "string",
                    "enum": ["prompt", "tool_schema", "tool_impl", "config", "test", "docs"],
                },
                "target": {"type": "string", "description": "Exact file/symbol/tool/config key."},
                "current_behavior": {
                    "type": "string",
                    "description": "Observed current behavior with evidence; do not guess.",
                },
                "root_cause": {
                    "type": "string",
                    "description": "Mechanism that explains why the current behavior fails.",
                },
                "alternatives": {
                    "type": "array",
                    "minItems": 2,
                    "maxItems": 5,
                    "items": {
                        "type": "object",
                        "properties": {
                            "option": {"type": "string"},
                            "description": {"type": "string"},
                            "tradeoff": {"type": "string"},
                        },
                        "required": ["option", "description", "tradeoff"],
                    },
                },
                "proposal": {
                    "type": "string",
                    "description": "Selected change description or unified diff. No secrets.",
                },
                "expected_metric": {
                    "type": "object",
                    "properties": {
                        "metric": {"type": "string"},
                        "baseline": {"type": "string"},
                        "target": {"type": "string"},
                        "method": {"type": "string"},
                    },
                    "required": ["metric", "baseline", "target", "method"],
                },
                "replay_plan": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Commands / cases that reproduce the failure and validate the change.",
                },
                "risk": {"type": "string", "enum": ["low", "medium", "high"]},
                "rollback": {
                    "type": "string",
                    "description": "Exact rollback: revert commit, restore config, etc.",
                },
            },
            "required": [
                "target_kind",
                "target",
                "current_behavior",
                "root_cause",
                "alternatives",
                "proposal",
                "expected_metric",
                "replay_plan",
                "risk",
                "rollback",
            ],
        },
        side_effect=True,
    )

    def __init__(
        self,
        *,
        store: EvolutionStore,
        root: Path | None = None,
        opinions: OpinionLog | None = None,
    ) -> None:
        self._store = store
        self._root = root
        self._opinions = opinions

    def invoke(self, call: ToolCall) -> ToolResult:
        started = time.perf_counter()
        try:
            proposal = ChangeProposal.from_payload(call.args, source_call=call.call_id)
            self._validate_target(proposal.target)
            self._store.queue(proposal)
            opinion_path = None
            opinion_error = None
            if self._opinions is not None:
                try:
                    opinion_path = str(self._opinions.record(proposal.to_payload()))
                except OSError as exc:
                    opinion_error = str(exc)
        except EvolutionError as exc:
            return _fail(call, ErrorCode.BAD_ARGS, str(exc), started)
        except OSError as exc:
            return _fail(call, ErrorCode.INTERNAL, f"proposal queue failed: {exc}", started)
        return ToolResult(
            call_id=call.call_id,
            ok=True,
            data={
                "queued": True,
                "proposal_id": proposal.proposal_id,
                "schema_version": proposal.schema_version,
                "status": proposal.status,
                "path": str(self._store.path),
                "proposal": proposal.to_payload(),
                "opinion_path": opinion_path,
                "opinion_error": opinion_error,
            },
            elapsed_s=time.perf_counter() - started,
        )


    def _validate_target(self, target: str) -> None:
        """拒绝明显不存在的文件路径；符号目标（如 knowledge.search）跳过。"""
        if self._root is None:
            return
        path_text = target.split(":", 1)[0]
        if not (path_text.endswith(".py") or "/" in path_text or path_text.endswith(".toml")):
            return
        candidate = Path(path_text)
        if not candidate.is_absolute():
            candidate = self._root / candidate
        if not candidate.exists():
            raise EvolutionError(f"target path does not exist: {target}")


def _fail(call: ToolCall, code: ErrorCode, message: str, started: float) -> ToolResult:
    return ToolResult(
        call_id=call.call_id,
        ok=False,
        error=ErrorInfo(code=code, message=message),
        elapsed_s=time.perf_counter() - started,
    )
