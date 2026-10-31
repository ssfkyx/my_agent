"""protocol — 组件间唯一的 JSON 信封与工具调用契约。

Module: agent.core.protocol
Responsibility: 定义 Envelope / MessageKind / ToolSpec / ToolCall / ToolResult / ErrorInfo
    及其编解码与校验。本模块不做任何 IO，不依赖任何其他业务模块。
Depends on: stdlib only (dataclasses, enum, json, re, typing)
Depended by: core.turn, core.plan, core.harness, adapters.llama_cpp, tools.*, ui.*
ADRs: ADR-001（唯一信封协议）, ADR-003（FP64：elapsed_s 为 binary64）, ADR-006（kind 只列已实现者）,
    ADR-021（失败预算与 ABORTED 错误码）, ADR-023（ROLE_SWITCH 事件）
"""

import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping

# 工具名形状在协议层定死，避免出现 "Read File" 这类无法路由的别名。
_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")


class ProtocolError(ValueError):
    """信封/工具契约被违反。

    前置条件：无。
    后置条件：异常对象携带可读的字段级说明，调用方不得静默吞掉。
    禁止事项：不要用它包装业务失败（业务失败用 ToolResult.error）。
    异常来源：字段缺失、类型错误、枚举越界、工具名不合规范。
    """


class MessageKind(StrEnum):
    """ledger 中允许出现的事件种类。

    只列已实现者；新增种类必须同时修改 Envelope 编解码并在 ADR 中记录（ADR-006/ADR-010）。
    """

    USER_INPUT = "USER_INPUT"
    PLAN = "PLAN"
    RETRY = "RETRY"
    TOOL_CALL = "TOOL_CALL"
    TOOL_RESULT = "TOOL_RESULT"
    REVIEW = "REVIEW"
    REFLECTION = "REFLECTION"
    CONSENT_REQUEST = "CONSENT_REQUEST"
    ROLE_SWITCH = "ROLE_SWITCH"
    ANSWER = "ANSWER"
    RUN_REPORT = "RUN_REPORT"
    ERROR = "ERROR"


class ErrorCode(StrEnum):
    """工具失败的固定错误码。

    重试与否由代码（而非模型）根据 retryable 判定，见 ADR-005。
    """

    BAD_ARGS = "BAD_ARGS"
    PLAN_INVALID = "PLAN_INVALID"
    NOT_FOUND = "NOT_FOUND"
    TIMEOUT = "TIMEOUT"
    DENIED_BY_GUARDRAIL = "DENIED_BY_GUARDRAIL"
    UPSTREAM_ERROR = "UPSTREAM_ERROR"
    MISSING_MMPROJ = "MISSING_MMPROJ"
    INTERNAL = "INTERNAL"
    # 用户拒绝升级或显式中止：本轮不再调用任何模型（ADR-021）。
    ABORTED = "ABORTED"


def require_mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    """断言 value 是 JSON 对象。

    前置条件：value 可来自任意反序列化结果。
    后置条件：返回 Mapping；绝不返回 None。
    禁止事项：不要在此做深拷贝（payload 由调用方自行冻结）。
    异常：ProtocolError（value 不是 Mapping）。
    """
    if not isinstance(value, Mapping):
        raise ProtocolError(f"{field_name} must be a JSON object, got {type(value).__name__}")
    return value


def require_text(value: Any, field_name: str) -> str:
    """断言 value 是非空字符串。

    前置条件：无。
    后置条件：返回去除首尾空白后仍非空的字符串。
    禁止事项：不要在这里做转义或截断。
    异常：ProtocolError（非字符串或全空白）。
    """
    if not isinstance(value, str) or not value.strip():
        raise ProtocolError(f"{field_name} must be a non-empty string")
    return value


@dataclass(frozen=True, slots=True)
class Envelope:
    """所有组件之间传递的唯一信封（ADR-001）。

    前置条件：kind 必须是 MessageKind 成员；message_id/turn_id/sender 非空。
    后置条件：payload 与 meta 均为 JSON 对象；meta 只承载旁路信息（seq/ts/role）。
    禁止事项：禁止把语义写进 meta；禁止跨 turn 复用 message_id。
    """

    kind: MessageKind
    message_id: str
    turn_id: str
    sender: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    meta: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.kind, MessageKind):
            raise ProtocolError(f"kind must be MessageKind, got {self.kind!r}")
        require_text(self.message_id, "message_id")
        require_text(self.turn_id, "turn_id")
        require_text(self.sender, "sender")
        require_mapping(self.payload, "payload")
        require_mapping(self.meta, "meta")

    def to_json(self) -> dict[str, Any]:
        """序列化为可落盘/可传输的 dict。

        前置条件：对象已通过 __post_init__ 校验。
        后置条件：返回新 dict，修改它不影响本对象。
        禁止事项：不要在此加入时间戳等易变字段（时间放 meta，由创建方决定）。
        异常：无。
        """
        return {
            "kind": str(self.kind),
            "message_id": self.message_id,
            "turn_id": self.turn_id,
            "sender": self.sender,
            "payload": dict(self.payload),
            "meta": dict(self.meta),
        }

    def to_json_line(self) -> str:
        """序列化为单行 JSON（JSONL 用）。

        前置条件：同 to_json。
        后置条件：返回不含换行的 UTF-8 兼容字符串。
        禁止事项：不要用于人类阅读输出（单行紧凑、无缩进，不适合直接展示）。
        异常：无。
        """
        return json.dumps(self.to_json(), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "Envelope":
        """从 dict 还原信封，并做完整校验。

        前置条件：data 应来自 json.loads 或 to_json 的往返。
        后置条件：返回等价 Envelope；字段一个都不少。
        禁止事项：不要对缺失字段做默认填充（会把协议漂移藏起来）。
        异常：ProtocolError（缺字段、空字符串、未知 kind、payload/meta 非对象）。
        """
        data = require_mapping(data, "envelope")
        kind_text = require_text(data.get("kind"), "kind")
        try:
            kind = MessageKind(kind_text)
        except ValueError as exc:
            raise ProtocolError(f"unknown kind: {kind_text}") from exc
        return cls(
            kind=kind,
            message_id=require_text(data.get("message_id"), "message_id"),
            turn_id=require_text(data.get("turn_id"), "turn_id"),
            sender=require_text(data.get("sender"), "sender"),
            payload=require_mapping(data.get("payload"), "payload"),
            meta=require_mapping(data.get("meta", {}), "meta"),
        )


@dataclass(frozen=True, slots=True)
class ErrorInfo:
    """工具失败的统一描述（ADR-001）。

    前置条件：code 必须是 ErrorCode 成员。
    后置条件：retryable 决定上层是否重试；message 面向人类，hint 面向下一步动作。
    禁止事项：不要在 message 里塞堆栈（堆栈进日志）。
    """

    code: ErrorCode
    message: str
    retryable: bool = False
    hint: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.code, ErrorCode):
            raise ProtocolError(f"error code must be ErrorCode, got {self.code!r}")
        require_text(self.message, "error.message")

    def to_payload(self) -> dict[str, Any]:
        """转为 payload 片段。

        前置条件：已通过 __post_init__。
        后置条件：返回新 dict，键固定为 code/message/retryable/hint。
        禁止事项：不要附加额外键（会破坏 ToolResult 的对称解析）。
        异常：无。
        """
        return {
            "code": str(self.code),
            "message": self.message,
            "retryable": self.retryable,
            "hint": self.hint,
        }

    @classmethod
    def from_payload(cls, data: Mapping[str, Any]) -> "ErrorInfo":
        """从 payload 片段还原。

        前置条件：data 为 dict。
        后置条件：返回 ErrorInfo。
        禁止事项：不要为未知 code 猜测映射。
        异常：ProtocolError（code 未知或 message 为空）。
        """
        data = require_mapping(data, "error")
        code_text = require_text(data.get("code"), "error.code")
        try:
            code = ErrorCode(code_text)
        except ValueError as exc:
            raise ProtocolError(f"unknown error code: {code_text}") from exc
        return cls(
            code=code,
            message=require_text(data.get("message"), "error.message"),
            retryable=bool(data.get("retryable", False)),
            hint=str(data.get("hint", "")),
        )


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """工具自描述：主 agent 选工具时看到的全部信息（ADR-006）。

    前置条件：name 形如 `category.verb_object`，全小写蛇形。
    后置条件：args_schema 是 JSON Schema 子集，可被直接塞进提示词。
    禁止事项：禁止在 description 里写实现细节（模型只需要"何时用"）。
    """

    name: str
    category: str
    description: str
    args_schema: Mapping[str, Any]
    side_effect: bool = False

    def __post_init__(self) -> None:
        if not _TOOL_NAME.match(self.name):
            raise ProtocolError(f"tool name must look like 'category.verb_object': {self.name!r}")
        require_text(self.category, "category")
        require_text(self.description, "description")
        require_mapping(self.args_schema, "args_schema")

    def to_payload(self) -> dict[str, Any]:
        """转为提示词/序列化用的 dict。

        前置条件：已通过 __post_init__。
        后置条件：返回新 dict。
        禁止事项：不要在返回中省略 side_effect（guardrail 依赖它）。
        异常：无。
        """
        return {
            "name": self.name,
            "category": self.category,
            "description": self.description,
            "args_schema": dict(self.args_schema),
            "side_effect": self.side_effect,
        }


@dataclass(frozen=True, slots=True)
class ToolCall:
    """一次工具调用的请求（ADR-001）。

    前置条件：call_id 在单个 turn 内唯一；tool 必须已在 ToolRegistry 注册。
    后置条件：args 恒为 JSON 对象（可为空）。
    禁止事项：禁止由模型直接指定 call_id（由 harness 生成）。
    """

    call_id: str
    tool: str
    args: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_text(self.call_id, "call_id")
        require_text(self.tool, "tool")
        require_mapping(self.args, "args")

    def to_payload(self) -> dict[str, Any]:
        """转为 TOOL_CALL 信封的 payload。

        前置条件：已通过 __post_init__。
        后置条件：返回新 dict。
        禁止事项：不要在此注入 step_id（由 harness 放 meta）。
        异常：无。
        """
        return {"call_id": self.call_id, "tool": self.tool, "args": dict(self.args)}

    @classmethod
    def from_payload(cls, data: Mapping[str, Any]) -> "ToolCall":
        """从 TOOL_CALL payload 还原。

        前置条件：data 为 dict。
        后置条件：返回 ToolCall。
        禁止事项：不要为缺失 args 编造默认值以外的推断。
        异常：ProtocolError。
        """
        data = require_mapping(data, "tool_call")
        return cls(
            call_id=require_text(data.get("call_id"), "call_id"),
            tool=require_text(data.get("tool"), "tool"),
            args=require_mapping(data.get("args", {}), "args"),
        )


@dataclass(frozen=True, slots=True)
class ToolResult:
    """一次工具调用的回执（ADR-001）。

    前置条件：ok=True 时 error 必须为 None；ok=False 时 error 必须存在。
    后置条件：elapsed_s 为 FP64 秒（ADR-003），可用于 RunReport。
    禁止事项：禁止用异常表达"工具业务失败"（见 ADR-005）。
    """

    call_id: str
    ok: bool
    data: Mapping[str, Any] = field(default_factory=dict)
    error: ErrorInfo | None = None
    elapsed_s: float = 0.0

    def __post_init__(self) -> None:
        require_text(self.call_id, "call_id")
        if not isinstance(self.ok, bool):
            raise ProtocolError("ok must be a bool")
        require_mapping(self.data, "data")
        if self.ok and self.error is not None:
            raise ProtocolError("ok result must not carry an error")
        if not self.ok and self.error is None:
            raise ProtocolError("failed result must carry an error")
        if not isinstance(self.elapsed_s, float):
            raise ProtocolError("elapsed_s must be a float (FP64)")

    def to_payload(self) -> dict[str, Any]:
        """转为 TOOL_RESULT 信封的 payload。

        前置条件：已通过 __post_init__。
        后置条件：error 为 None 时序列化为 null（保持结构对称）。
        禁止事项：不要省略 error 键。
        异常：无。
        """
        return {
            "call_id": self.call_id,
            "ok": self.ok,
            "data": dict(self.data),
            "error": None if self.error is None else self.error.to_payload(),
            "elapsed_s": self.elapsed_s,
        }

    @classmethod
    def from_payload(cls, data: Mapping[str, Any]) -> "ToolResult":
        """从 TOOL_RESULT payload 还原。

        前置条件：data 为 dict。
        后置条件：返回 ToolResult。
        禁止事项：不要把未知字段悄悄忽略后当作成功。
        异常：ProtocolError。
        """
        data = require_mapping(data, "tool_result")
        raw_error = data.get("error")
        return cls(
            call_id=require_text(data.get("call_id"), "call_id"),
            ok=bool(data.get("ok", False)),
            data=require_mapping(data.get("data", {}), "data"),
            error=None if raw_error is None else ErrorInfo.from_payload(raw_error),
            elapsed_s=float(data.get("elapsed_s", 0.0)),
        )
