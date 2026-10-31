"""reflection — 次 agent 的反思与长期记忆条目。

Module: agent.core.reflection
Responsibility: 把"这一轮做得怎样、下次怎么改"压成可注入计划提示的短句（lessons/plan_hint/tool_notes）。
Depends on: stdlib (json, time, dataclasses) + agent.core.{protocol,plan,learning} + agent.ports
Depended by: core.harness, adapters.memory
ADRs: ADR-018（反思与只追加记忆）, ADR-015（关闭思考）, ADR-014（role 路由）, ADR-039（change_request）
"""

import json
import time
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from agent.core.learning import Review
from agent.core.plan import Plan
from agent.core.protocol import ProtocolError, ToolResult, require_mapping, require_text
from agent.ports import BrainPort, BrainRequest

_MAX_LESSON_CHARS = 200


class ReflectionError(ProtocolError):
    """反思结果不合法。

    前置条件：无。
    后置条件：异常信息指出具体字段。
    禁止事项：不要用它表达"这轮失败了"（那是 verdict/Review 的职责）。
    异常来源：Reflection.from_payload。
    """


@dataclass(frozen=True, slots=True)
class Reflection:
    """一段可复用的经验。

    前置条件：lessons 非空（没有教训就不要写记忆）；每条 <= 200 字符。
    后置条件：to_payload 可直接追加进 memory.jsonl。
    禁止事项：不要把工具输出原文塞进来（记忆是结论，不是日志）。
    """

    turn_id: str
    verdict: str
    lessons: tuple[str, ...]
    plan_hint: str
    tool_notes: tuple[str, ...]
    created_at: float
    change_request: str = ""

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any], *, turn_id: str, verdict: str) -> "Reflection":
        """校验并构造 Reflection。

        前置条件：payload 来自受 schema 约束的解码。
        后置条件：lessons 去重且截断；plan_hint 可为空字符串。
        禁止事项：不要为缺失 lessons 补默认值（空即表示模型没给结论，应当放弃记忆）。
        异常：ReflectionError。
        """
        data = require_mapping(payload, "reflection")
        lessons = _text_list(data.get("lessons"), "reflection.lessons")
        if not lessons:
            raise ReflectionError("reflection.lessons must contain at least one lesson")
        plan_hint = data.get("plan_hint", "")
        if not isinstance(plan_hint, str):
            raise ReflectionError("reflection.plan_hint must be a string")
        return cls(
            turn_id=turn_id,
            verdict=verdict,
            lessons=lessons,
            plan_hint=plan_hint.strip()[: _MAX_LESSON_CHARS * 2],
            tool_notes=_text_list(data.get("tool_notes"), "reflection.tool_notes"),
            created_at=time.time(),
            change_request=str(data.get("change_request", "")).strip()[:500],
        )

    def to_payload(self) -> dict[str, Any]:
        """转为 JSONL 行内容。

        前置条件：对象已通过 from_payload 构造。
        后置条件：返回可 JSON 序列化的 dict（FP64 时间戳）。
        禁止事项：不要在 payload 中放不可序列化对象。
        异常：无。
        """
        return {
            "turn_id": self.turn_id,
            "verdict": self.verdict,
            "lessons": list(self.lessons),
            "plan_hint": self.plan_hint,
            "tool_notes": list(self.tool_notes),
            "created_at": self.created_at,
            "change_request": self.change_request,
        }


_REFLECTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "lessons": {"type": "array", "items": {"type": "string"}},
        "plan_hint": {"type": "string"},
        "tool_notes": {"type": "array", "items": {"type": "string"}},
        "change_request": {"type": "string", "maxLength": 500},
    },
    "required": ["lessons", "plan_hint", "tool_notes"],
}

_REFLECTION_PROMPT = """You are the post-run reviewer of a local agent. Extract transferable lessons
for the NEXT run. Reply with JSON only. Do not restate the answer; do not invent facts.

Rules:
- lessons: 1-3 short imperative sentences about planning or tool usage (max 200 chars each).
- plan_hint: one sentence on how to plan this kind of request better next time (may be empty).
- tool_notes: 0-2 sentences on which tool to use or avoid for this kind of request.
- If everything went well, say what to keep doing (still 1-3 lessons).
- change_request: if a concrete harness/tool/prompt change would prevent this failure, state it in one sentence;
  otherwise use an empty string. Never propose disabling guardrails or tests.

User request:
{user_text}

Plan (JSON):
{plan}

Tool results summary:
{results}

Review verdict: {verdict} (score {score:.2f}); issues: {issues}

Draft answer:
{draft}"""


class Reflector:
    """用次脑（role="review"）生成一条 Reflection（ADR-018）。

    前置条件：brain 支持 role="review"。
    后置条件：reflect() 返回校验过的 Reflection；失败时抛异常，由 harness 忽略（best-effort）。
    禁止事项：不要在此写文件（存储由 MemoryPort 负责）。
    """

    def __init__(self, *, brain: BrainPort, temperature: float, max_tokens: int) -> None:
        self._brain = brain
        self._temperature = temperature
        self._max_tokens = max_tokens

    def reflect(
        self,
        *,
        turn_id: str,
        user_text: str,
        plan: Plan,
        results: Sequence[ToolResult],
        draft_answer: str,
        review: Review | None,
    ) -> Reflection:
        """生成一轮的反思。

        前置条件：review 可为 None（评审不可用时仍可反思）。
        后置条件：返回 Reflection（lessons 至少一条）。
        禁止事项：不要注入历史记忆（会自我强化旧结论）。
        异常：BrainError（调用失败）、ReflectionError（结构非法）。
        """
        summary = [
            {"call_id": item.call_id, "ok": item.ok, "error": None if item.error is None else item.error.code}
            for item in results
        ]
        verdict = "UNKNOWN" if review is None else str(review.verdict)
        score = 0.0 if review is None else review.score
        issues = "" if review is None or not review.issues else "; ".join(review.issues)
        prompt = _REFLECTION_PROMPT.format(
            user_text=user_text,
            plan=json.dumps(plan.to_payload(), ensure_ascii=False, indent=2),
            results=json.dumps(summary, ensure_ascii=False),
            verdict=verdict,
            score=score,
            issues=issues or "none",
            draft=draft_answer,
        )
        reply = self._brain.complete(
            BrainRequest(
                messages=(
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": "Write the JSON reflection now."},
                ),
                json_schema=_REFLECTION_SCHEMA,
                temperature=self._temperature,
                max_tokens=self._max_tokens,
                role="review",
                disable_thinking=True,
            )
        )
        return Reflection.from_payload(reply.content, turn_id=turn_id, verdict=verdict)


def _text_list(value: Any, field_name: str) -> tuple[str, ...]:
    """把 JSON 数组校验成去重、截断后的字符串元组。

    前置条件：value 应为 list/tuple。
    后置条件：保持出现顺序去重；每条裁剪到 _MAX_LESSON_CHARS。
    禁止事项：不要吞掉非字符串元素（抛错更利于发现模型退化）。
    异常：ReflectionError。
    """
    if not isinstance(value, (list, tuple)):
        raise ReflectionError(f"{field_name} must be an array")
    seen: set[str] = set()
    items: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise ReflectionError(f"{field_name}[{index}] must be a string")
        text = item.strip()[:_MAX_LESSON_CHARS]
        if text and text not in seen:
            seen.add(text)
            items.append(text)
    return tuple(items)


def lessons_from(entries: Sequence[Mapping[str, Any]], limit: int) -> tuple[str, ...]:
    """从记忆条目中抽取最近 lessons（用于注入计划提示）。

    前置条件：entries 为按时间升序的 memory 记录；limit >= 0。
    后置条件：返回至多 limit 条短句（去重、保持最近优先）。
    禁止事项：不要把 plan_hint 混进 lessons（两者用途不同）。
    异常：无。
    """
    if limit <= 0:
        return ()
    collected: list[str] = []
    for entry in reversed(list(entries)):
        for lesson in entry.get("lessons", ()):  # 最近一轮优先
            if isinstance(lesson, str) and lesson.strip():
                collected.append(require_text(lesson, "lesson").strip())
            if len(collected) >= limit:
                return tuple(collected)
    return tuple(collected)
