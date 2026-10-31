"""plan — 计划与步骤的值对象及其校验。

Module: agent.core.plan
Responsibility: 定义 StepKind / Step / Plan，并把模型返回的 payload 校验成可执行对象。
Depends on: stdlib (dataclasses, enum, typing) + agent.core.protocol
Depended by: core.harness, (后续) core.report
ADRs: ADR-005（Step 顺序流水线与 why 必填）, ADR-001（payload 形状）
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping

from agent.core.protocol import ProtocolError, require_mapping, require_text


class PlanError(ProtocolError):
    """计划不合法（模型输出了结构上无法执行的内容）。

    前置条件：无。
    后置条件：异常信息指出出错的字段路径，便于排障。
    禁止事项：不要用 PlanError 表示"计划执行失败"（那是 ToolResult 的职责）。
    异常来源：Plan.from_payload 的字段校验。
    """


class StepKind(StrEnum):
    """步骤种类。ADR-006 只放行 tb-0 需要的两种。"""

    TOOL = "tool"
    ANSWER = "answer"


@dataclass(frozen=True, slots=True)
class Step:
    """计划中的最小执行单元。

    前置条件：kind=TOOL 时 target 必须是已注册工具名；kind=ANSWER 时 target 可为空串。
    后置条件：step_id 由 Plan.from_payload 统一生成（s-1..s-n），不采信模型给的 id。
    禁止事项：禁止在 args 里放不可 JSON 序列化的对象。
    """

    step_id: str
    kind: StepKind
    target: str
    args: Mapping[str, Any]
    why: str


@dataclass(frozen=True, slots=True)
class Plan:
    """有序计划：目标 + 决策理由 + 步骤序列。

    前置条件：steps 非空。
    后置条件：steps 不可变（tuple），顺序即执行顺序。
    禁止事项：禁止跳过 rationale/why（需求要求"记录决策原因"）。
    """

    goal: str
    rationale: str
    steps: tuple[Step, ...]

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "Plan":
        """把模型返回的 JSON 校验成 Plan。

        前置条件：payload 来自受 json_schema 约束的解码结果。
        后置条件：返回 step_id 规范化的 Plan；tool 步骤的 target 非空。
        禁止事项：不要再这里做"猜测性修复"（例如把 steps 缺失补成空计划）；
            非法即抛，由 harness 转成 ERROR 信封。
        异常：PlanError（steps 缺失/为空/元素非对象/kind 未知/TOOL 缺 target）。
        """
        data = require_mapping(payload, "plan")
        goal = require_text(data.get("goal"), "plan.goal")
        rationale = require_text(data.get("rationale"), "plan.rationale")
        raw_steps = data.get("steps")
        if not isinstance(raw_steps, (list, tuple)) or not raw_steps:
            raise PlanError("plan.steps must be a non-empty array")

        steps: list[Step] = []
        for index, raw_step in enumerate(raw_steps, start=1):
            step_data = require_mapping(raw_step, f"plan.steps[{index}]")
            kind_text = require_text(step_data.get("kind"), f"plan.steps[{index}].kind")
            try:
                kind = StepKind(kind_text.strip().lower())
            except ValueError as exc:
                raise PlanError(f"plan.steps[{index}].kind must be 'tool' or 'answer'") from exc
            target = str(step_data.get("target", "")).strip()
            if kind is StepKind.TOOL and not target:
                raise PlanError(f"plan.steps[{index}].target is required for tool steps")
            steps.append(
                Step(
                    step_id=f"s-{index}",
                    kind=kind,
                    target=target,
                    args=dict(require_mapping(step_data.get("args", {}), f"plan.steps[{index}].args")),
                    why=require_text(step_data.get("why"), f"plan.steps[{index}].why"),
                )
            )
        return cls(goal=goal, rationale=rationale, steps=tuple(steps))

    def to_payload(self) -> dict[str, Any]:
        """转为 PLAN 信封的 payload。

        前置条件：Plan 已通过 from_payload 构造。
        后置条件：返回可 JSON 序列化的 dict，字段与模型契约一致。
        禁止事项：不要包含 step_id（它是本地执行序号，不是模型语义）。
        异常：无。
        """
        return {
            "goal": self.goal,
            "rationale": self.rationale,
            "steps": [
                {"kind": str(step.kind), "target": step.target, "args": dict(step.args), "why": step.why}
                for step in self.steps
            ],
        }
