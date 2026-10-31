"""budget — 单步失败预算与跨轮进展指纹。

Module: agent.core.budget
Responsibility: 记录同一计划步骤的失败次数，决定“继续重试 / 暂停询问用户”；
    ProgressWatch 用计划指纹发现“多轮无进展”，为升级建议提供依据。
Depends on: stdlib (dataclasses, hashlib) + agent.core.protocol
Depended by: core.harness
ADRs: ADR-021（失败预算与升级阈值）, ADR-009（护栏拒绝不消耗重试预算）
"""

import hashlib
from dataclasses import dataclass, field
from typing import Sequence

from agent.core.protocol import ErrorInfo


@dataclass(slots=True)
class TurnBudget:
    """一个步骤的失败预算（默认最多 3 次尝试，含首次）。

    前置条件：max_attempts >= 1；attempts 为已记录的失败次数。
    后置条件：record_failure 返回“是否还允许再试一次”；exhausted 表示预算已耗尽。
    禁止事项：不要把不可重试错误也计入预算（它们应立即停，见 ADR-009/021）。
    """

    max_attempts: int = 3
    attempts: int = 0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.attempts < 0:
            raise ValueError("attempts must be >= 0")
        if self.attempts > self.max_attempts:
            raise ValueError("attempts must not exceed max_attempts")

    @property
    def exhausted(self) -> bool:
        """预算是否已耗尽。

        前置条件：无。
        后置条件：attempts >= max_attempts 时为 True。
        禁止事项：不要在未记录失败前用它判断“是否该重试”。
        异常：无。
        """
        return self.attempts >= self.max_attempts

    @property
    def remaining(self) -> int:
        """还能再尝试几次（不把当前这次算进去）。

        前置条件：无。
        后置条件：返回 [0, max_attempts] 内的整数。
        禁止事项：不要用它代替 exhausted 判断（两者语义不同）。
        异常：无。
        """
        return max(0, self.max_attempts - self.attempts)

    def consume(self) -> int:
        """记录一次尝试并返回累计次数（按“尝试”计预算的调用方使用）。

        前置条件：无。
        后置条件：attempts +1；返回新的 attempts。
        禁止事项：不要与 record_failure 同时用于同一次失败（会重复计数）。
        异常：无。
        """
        self.attempts += 1
        return self.attempts

    def record_failure(self, error: ErrorInfo | None = None) -> bool:
        """记录一次失败，并返回是否允许重试。

        前置条件：error 为 None 时视为可重试；否则读取 error.retryable。
        后置条件：attempts +1；返回 should_retry 的结论。
        禁止事项：不要对 DENIED_BY_GUARDRAIL 等确定性失败调用本方法。
        异常：无。
        """
        self.attempts += 1
        if error is None:
            return not self.exhausted
        return self.should_retry(error)

    def should_retry(self, error: ErrorInfo) -> bool:
        """判断给定错误是否应再次尝试。

        前置条件：error 来自刚才的 ToolResult。
        后置条件：仅当 error.retryable 且预算未耗尽时为 True。
        禁止事项：不要让模型决定 retryable（由错误码/适配器决定，ADR-005）。
        异常：无。
        """
        return bool(error.retryable) and not self.exhausted


@dataclass(slots=True)
class ProgressWatch:
    """按“意图 + 工具序列”指纹统计重复轮次的观察器（ADR-021 的升级检查点）。

    前置条件：escalation_rounds >= 1。
    后置条件：note 返回该指纹累计出现的轮次数；stalled 表示达到升级阈值。
    禁止事项：不要把指纹当安全哈希（这里只用于去重，不用于鉴权）。
    """

    escalation_rounds: int = 3
    _seen: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.escalation_rounds < 1:
            raise ValueError("escalation_rounds must be >= 1")

    def note(self, fingerprint: str) -> int:
        """记录一次指纹出现，并返回累计轮次。

        前置条件：fingerprint 非空。
        后置条件：该指纹计数 +1。
        禁止事项：不要在失败步骤内部调用（轮次粒度是 Turn，不是 attempt）。
        异常：ValueError（空指纹）。
        """
        if not fingerprint.strip():
            raise ValueError("fingerprint must be non-empty")
        count = self._seen.get(fingerprint, 0) + 1
        self._seen[fingerprint] = count
        return count

    def rounds_for(self, fingerprint: str) -> int:
        """返回指纹已出现的轮次数（未知指纹为 0）。

        前置条件：无。
        后置条件：返回非负整数。
        禁止事项：不要在查询时隐式创建记录。
        异常：无。
        """
        return self._seen.get(fingerprint, 0)

    def stalled(self, fingerprint: str) -> bool:
        """指纹是否已达到升级阈值。

        前置条件：无。
        后置条件：rounds_for >= escalation_rounds 时为 True。
        禁止事项：不要因为单次失败就判 stalled（阈值是跨轮概念）。
        异常：无。
        """
        return self.rounds_for(fingerprint) >= self.escalation_rounds

    @staticmethod
    def fingerprint(intent: str, tool_sequence: Sequence[str]) -> str:
        """生成“意图 + 工具序列”的稳定指纹。

        前置条件：intent 非空；tool_sequence 可为空。
        后置条件：返回 40 位十六进制 SHA-1；同一输入恒得同一值。
        禁止事项：不要包含时间戳或随机数（否则永远无法判重）。
        异常：无。
        """
        material = intent.strip() + "\n" + "\n".join(str(tool) for tool in tool_sequence)
        return hashlib.sha1(material.encode("utf-8")).hexdigest()
