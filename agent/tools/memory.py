"""memory — 长期记忆与"改工具"请愿队列的工具面。

Module: agent.tools.memory
Responsibility: 让主 agent 能读回最近记忆、写入人工经验、把变更诉求排进队列（不自动生效）。
Depends on: stdlib (time) + agent.core.protocol + agent.ports
Depended by: app（注册）
ADRs: ADR-018（只追加与入队语义）
"""

import time

from agent.core.protocol import ErrorCode, ErrorInfo, ToolCall, ToolResult, ToolSpec
from agent.ports import MemoryPort


class RecallTool:
    """读回最近的记忆条目。

    前置条件：无。
    后置条件：data.entries 为按时间升序的记录。
    禁止事项：不要把整份记忆一次性倒给模型（默认只给最近 3 条）。
    """

    spec = ToolSpec(
        name="memory.recall",
        category="memory",
        description="Recall the most recent lessons recorded by previous runs.",
        args_schema={
            "type": "object",
            "properties": {"limit": {"type": "integer", "description": "How many entries (default 3)."}},
            "required": [],
        },
        side_effect=False,
    )

    def __init__(self, *, memory: MemoryPort, default_limit: int) -> None:
        self._memory = memory
        self._default_limit = default_limit

    def invoke(self, call: ToolCall) -> ToolResult:
        """执行读回。

        前置条件：limit 缺省时用配置值。
        后置条件：ok=True 时 data.entries 为列表（可能为空）。
        禁止事项：不要在空记忆时报错。
        异常：无。
        """
        started = time.perf_counter()
        raw_limit = call.args.get("limit", self._default_limit)
        if not isinstance(raw_limit, int) or isinstance(raw_limit, bool):
            return _fail(call, "args.limit must be an integer", started)
        limit = min(max(raw_limit, 1), 20)
        try:
            entries = self._memory.recent(limit)
        except OSError as exc:
            return _fail(call, f"recall failed: {exc}", started)
        return ToolResult(
            call_id=call.call_id,
            ok=True,
            data={"entries": [dict(entry) for entry in entries], "limit": limit},
            elapsed_s=time.perf_counter() - started,
        )


class RecordTool:
    """把一条人类经验写进长期记忆。

    前置条件：args.note 非空。
    后置条件：记忆文件追加一行（verdict=HUMAN）。
    禁止事项：不要用它改写已有记录或删除历史。
    """

    spec = ToolSpec(
        name="memory.record",
        category="memory",
        description=(
            "Record a lesson supplied by the user (or by yourself) into long-term memory. "
            "Use it when the user corrects you or states a durable preference."
        ),
        args_schema={
            "type": "object",
            "properties": {"note": {"type": "string", "description": "The durable lesson, one sentence."}},
            "required": ["note"],
        },
        side_effect=True,
    )

    def __init__(self, *, memory: MemoryPort) -> None:
        self._memory = memory

    def invoke(self, call: ToolCall) -> ToolResult:
        """写入一条人工经验。

        前置条件：note 为非空字符串。
        后置条件：ok=True 时 data.turn_id 为生成的记录 id。
        禁止事项：不要截断用户原话到失去含义（>200 字符直接拒绝）。
        异常：无。
        """
        started = time.perf_counter()
        note = call.args.get("note")
        if not isinstance(note, str) or not note.strip():
            return _fail(call, "args.note must be a non-empty string", started)
        text = note.strip()
        if len(text) > 200:
            return _fail(call, "args.note must be at most 200 characters", started)
        turn_id = f"manual-{int(time.time())}"
        entry = {
            "turn_id": turn_id,
            "verdict": "HUMAN",
            "lessons": [text],
            "plan_hint": "",
            "tool_notes": [],
            "created_at": time.time(),
        }
        try:
            self._memory.append(entry)
        except OSError as exc:
            return _fail(call, f"record failed: {exc}", started)
        return ToolResult(
            call_id=call.call_id,
            ok=True,
            data={"turn_id": turn_id, "lesson": text},
            elapsed_s=time.perf_counter() - started,
        )


class RequestToolChangeTool:
    """向 harness 提"改工具"的请求（入队，不生效）。

    前置条件：args.tool 与 args.reason 非空。
    后置条件：请求写入队列文件；工具注册表与提示词不变。
    禁止事项：不要在此直接调用或修改任何工具。
    """

    spec = ToolSpec(
        name="memory.request_tool_change",
        category="memory",
        description=(
            "Queue a request to change a tool (new args, better description, new capability). "
            "The harness reviews it later; nothing changes automatically."
        ),
        args_schema={
            "type": "object",
            "properties": {
                "tool": {"type": "string", "description": "Tool name that needs a change."},
                "reason": {"type": "string", "description": "Why it needs to change and what you propose."},
            },
            "required": ["tool", "reason"],
        },
        side_effect=True,
    )

    def __init__(self, *, memory: MemoryPort) -> None:
        self._memory = memory

    def invoke(self, call: ToolCall) -> ToolResult:
        """把请求写入队列。

        前置条件：tool/reason 均为非空字符串。
        后置条件：ok=True 且 data.queued=True。
        禁止事项：不要假装请求已经生效。
        异常：无。
        """
        started = time.perf_counter()
        tool = call.args.get("tool")
        reason = call.args.get("reason")
        if not isinstance(tool, str) or not tool.strip():
            return _fail(call, "args.tool must be a non-empty string", started)
        if not isinstance(reason, str) or not reason.strip():
            return _fail(call, "args.reason must be a non-empty string", started)
        payload = {"tool": tool.strip(), "reason": reason.strip(), "ts": time.time()}
        try:
            self._memory.record_tool_request(payload)
        except OSError as exc:
            return _fail(call, f"queue failed: {exc}", started)
        return ToolResult(
            call_id=call.call_id,
            ok=True,
            data={"queued": True, "tool": payload["tool"]},
            elapsed_s=time.perf_counter() - started,
        )


def _fail(call: ToolCall, message: str, started: float) -> ToolResult:
    """构造 BAD_ARGS/INTERNAL 失败回执。

    前置条件：started 为 perf_counter 起点。
    后置条件：返回 ok=False 的 ToolResult。
    禁止事项：不要吞掉 call_id。
    异常：无。
    """
    return ToolResult(
        call_id=call.call_id,
        ok=False,
        error=ErrorInfo(code=ErrorCode.BAD_ARGS, message=message, retryable=False, hint=""),
        elapsed_s=time.perf_counter() - started,
    )
