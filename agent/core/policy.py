"""policy — 运行期策略：工具护栏（路径沙箱）。

Module: agent.core.policy
Responsibility: 在执行工具前校验并归一化参数（路径沙箱、写边界、敏感名拒绝），产出与 CrewAI 对齐的
    GuardrailOutcome。纯校验，不做 IO，不调用工具。
Depends on: stdlib (dataclasses, pathlib, typing) + agent.core.protocol
Depended by: core.harness, app
ADRs: ADR-009（护栏与沙箱）, ADR-001（ErrorInfo/错误码）, ADR-005（失败以数据表达）,
    ADR-026（写路径限制在 write_root）
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from agent.core.protocol import ErrorCode, ErrorInfo, ToolCall, ToolSpec


@dataclass(frozen=True, slots=True)
class GuardrailPolicy:
    """护栏策略（来自配置）。

    前置条件：root 为已解析的绝对路径；deny_names 为小写比较用的名字集合（可空）。
    后置条件：不可变，可被多个 turn 复用。
    禁止事项：不要在运行期修改 deny_names（会让"同一计划两次执行结果不同"）。
    """

    root: Path
    deny_names: tuple[str, ...]
    # 写工具只能落在该子目录；None 表示禁用写工具（ADR-026）。
    write_root: Path | None = None

    def __post_init__(self) -> None:
        if not self.root.is_absolute():
            raise ValueError("guardrail root must be an absolute path")
        if any(not name.strip() for name in self.deny_names):
            raise ValueError("deny_names must not contain empty entries")
        if self.write_root is not None:
            if not self.write_root.is_absolute():
                raise ValueError("write_root must be an absolute path")
            if not self.write_root.is_relative_to(self.root):
                raise ValueError("write_root must be inside guardrail root")


@dataclass(frozen=True, slots=True)
class GuardrailOutcome:
    """一次护栏检查的结果（字段与 CrewAI 的 GuardrailResult 对齐，见 ADR-009）。

    前置条件：success=True 时 error 必须为 None；success=False 时 error 必须存在。
    后置条件：value 为**归一化后的参数**，调用方必须用它执行，而不是原始 args。
    禁止事项：不要把 value 当成"原始参数副本"修改后再执行。
    """

    success: bool
    value: Mapping[str, Any]
    error: ErrorInfo | None = None
    retries: int = 0

    def __post_init__(self) -> None:
        if self.success and self.error is not None:
            raise ValueError("successful outcome must not carry an error")
        if not self.success and self.error is None:
            raise ValueError("failed outcome must carry an error")
        if self.retries < 0:
            raise ValueError("retries must be >= 0")


class Guardrail:
    """工具调用护栏（ADR-009）。

    前置条件：policy.root 存在且为绝对路径。
    后置条件：check_tool 对同一 (call, spec) 恒返回同一结论（纯函数）。
    禁止事项：不要在此打开文件、探测路径是否存在（存在性由工具自己报 NOT_FOUND）。
    """

    def __init__(self, policy: GuardrailPolicy) -> None:
        self._policy = policy
        self._deny = frozenset(name.casefold() for name in policy.deny_names)

    def check_tool(self, call: ToolCall, spec: ToolSpec) -> GuardrailOutcome:
        """校验并归一化一次工具调用的参数。

        前置条件：call.tool == spec.name；spec 来自已注册工具。
        后置条件：带 path 参数的工具返回绝对路径；其余工具原样放行（value 为原 args 的副本）。
        禁止事项：不要为"看起来无害"的路径开例外；不要在失败时返回部分归一化结果。
        异常：无（所有拒绝都以 success=False 表达）。
        """
        properties = spec.args_schema.get("properties")
        if not isinstance(properties, Mapping) or "path" not in properties:
            return GuardrailOutcome(success=True, value=dict(call.args))

        raw_path = call.args.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            return self._deny_with(
                ErrorInfo(code=ErrorCode.BAD_ARGS, message="args.path must be a non-empty string")
            )

        try:
            resolved = self._resolve(raw_path)
        except _Denied as exc:
            return self._deny_with(exc.error)

        # 写工具（ADR-026）：只允许写入配置的 write_root 子目录。
        if spec.side_effect and spec.category == "workspace":
            write_root = self._policy.write_root
            if write_root is None:
                return self._deny_with(
                    ErrorInfo(
                        code=ErrorCode.DENIED_BY_GUARDRAIL,
                        message="workspace write tools are disabled by policy",
                        hint="set [workspace].write_root in config to enable writes",
                    )
                )
            if not resolved.is_relative_to(write_root):
                return self._deny_with(
                    ErrorInfo(
                        code=ErrorCode.DENIED_BY_GUARDRAIL,
                        message=f"write path is outside write_root: {raw_path}",
                        hint=f"write under {write_root}",
                    )
                )

        normalized: dict[str, Any] = dict(call.args)
        normalized["path"] = str(resolved)
        return GuardrailOutcome(success=True, value=normalized)

    def _resolve(self, raw_path: str) -> Path:
        """把用户给的路径解析并限制在 workspace root 内。

        前置条件：raw_path 为非空字符串（可含 ~、..、绝对路径）。
        后置条件：返回 root 内的绝对路径（已跟随符号链接）。
        禁止事项：不要用字符串前缀比较判断"在目录内"（`/root2` 会骗过 `/root` 前缀）。
        异常：_Denied（越界、命中拒绝名）。
        """
        root = self._policy.root
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        # resolve() 会折叠 .. 并跟随符号链接，保证后续判断面对的是真实位置。
        resolved = candidate.resolve()
        if not resolved.is_relative_to(root):
            raise _Denied(
                ErrorInfo(
                    code=ErrorCode.DENIED_BY_GUARDRAIL,
                    message=f"path escapes workspace root: {raw_path}",
                    hint=f"use a path inside {root}",
                )
            )

        parts = resolved.relative_to(root).parts
        for part in parts:
            if part.casefold() in self._deny:
                raise _Denied(
                    ErrorInfo(
                        code=ErrorCode.DENIED_BY_GUARDRAIL,
                        message=f"path segment is denied by policy: {part}",
                    )
                )
        return resolved

    @staticmethod
    def _deny_with(error: ErrorInfo) -> GuardrailOutcome:
        """构造拒绝结果。

        前置条件：error 描述了拒绝原因。
        后置条件：返回 success=False、retries=0 的结果（拒绝不消耗重试预算）。
        禁止事项：不要把 retryable 设为 True（策略性失败重试无意义）。
        异常：无。
        """
        return GuardrailOutcome(success=False, value={}, error=error, retries=0)


class _Denied(Exception):
    """内部信号：路径被沙箱拒绝。

    前置条件：仅在 Guardrail._resolve 内抛出。
    后置条件：携带可序列化的 ErrorInfo。
    禁止事项：不要让它逃出 Guardrail（check_tool 必须把它转成 outcome）。
    """

    def __init__(self, error: ErrorInfo) -> None:
        super().__init__(error.message)
        self.error = error
