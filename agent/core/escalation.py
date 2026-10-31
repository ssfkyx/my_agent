"""escalation — 失败预算耗尽后的场外升级编排。

Module: agent.core.escalation
Responsibility: 组装“原始输入 + 失败摘要”的升级 payload，经 Redactor 脱敏后交给 MasterPort；
    本模块不负责询问用户（同意是 Harness/UI 的职责），也不实现任何具体 HTTP 客户端。
Depends on: stdlib (typing) + agent.core.{budget,protocol,redact} + agent.ports
Depended by: core.harness
ADRs: ADR-021（升级必须经用户同意）, ADR-022（外发前脱敏）
"""

from typing import Any, Mapping, Sequence

from agent.core.budget import TurnBudget
from agent.core.protocol import ErrorInfo
from agent.core.redact import Redactor
from agent.ports import EscalationError, MasterPort


class Escalator:
    """把“本地无解”的失败上下文转成一次脱敏的场外请求。

    前置条件：master 实现 MasterPort；redactor 默认使用 ADR-022 的标准规则。
    后置条件：escalate 只向 master 发送脱敏后的 payload；失败以 EscalationError 表达。
    禁止事项：不要在这里直接读环境变量或拼 URL（那是 adapters.handle_to_master 的职责）。
    """

    def __init__(self, *, master: MasterPort, redactor: Redactor | None = None) -> None:
        self._master = master
        self._redactor = redactor if redactor is not None else Redactor()

    def needs_consent(self, budget: TurnBudget) -> bool:
        """预算是否已耗尽、必须征求用户同意。

        前置条件：budget 为当前步骤的预算。
        后置条件：返回 budget.exhausted。
        禁止事项：不要在预算未耗尽时询问（会打断正常的重试流程）。
        异常：无。
        """
        return budget.exhausted

    def build_payload(
        self,
        *,
        user_text: str,
        goal: str,
        tool: str,
        attempts: int,
        max_attempts: int,
        errors: Sequence[ErrorInfo],
    ) -> dict[str, Any]:
        """组装升级 payload（尚未脱敏）。

        前置条件：errors 非空；attempts >= 1；max_attempts >= 1。
        后置条件：返回只含 JSON 基本类型的 dict，包含原始输入、目标、工具与全部失败摘要。
        禁止事项：不要在这里脱敏（preview 与 escalate 必须走同一条 Redactor 路径）。
        异常：ValueError（errors 为空或计数非法）。
        """
        if not errors:
            raise ValueError("escalation requires at least one error")
        if attempts < 1 or max_attempts < 1:
            raise ValueError("attempts and max_attempts must be >= 1")
        return {
            "task": "handle_to_master",
            "user_request": user_text,
            "goal": goal,
            "tool": tool,
            "attempts": attempts,
            "max_attempts": max_attempts,
            "errors": [error.to_payload() for error in errors],
        }

    def preview(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """返回脱敏后的 payload 预览（用于 CONSENT_REQUEST）。

        前置条件：payload 来自 build_payload。
        后置条件：返回新 dict；不修改入参。
        禁止事项：不要把未脱敏的 payload 交给 UI 展示。
        异常：无。
        """
        redacted = self._redactor.redact_payload(payload)
        if not isinstance(redacted, dict):
            raise EscalationError("redacted payload must remain a JSON object")
        return redacted

    def escalate(self, payload: Mapping[str, Any]) -> str:
        """脱敏后调用场外主脑，并返回其答案文本。

        前置条件：用户已同意；payload 来自 build_payload。
        后置条件：返回去除首尾空白的非空字符串。
        禁止事项：不要在这里重试（一次同意只对应一次外发）；不要把原文再拼回去。
        异常：EscalationError（未配置、网络/HTTP 失败、远端返回空）。
        """
        redacted = self.preview(payload)
        text = self._master.complete(redacted)
        if not isinstance(text, str) or not text.strip():
            raise EscalationError("master returned an empty answer")
        return text.strip()
