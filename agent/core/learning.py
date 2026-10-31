"""learning — 次 agent 的评审（Review）与结论契约。

Module: agent.core.learning
Responsibility: 定义 Verdict/Review 的值与校验，并用 BrainPort（role="review"）产出评审意见。
    评审是建议性的：本模块不做返工、不改写答案、不阻断交付。
Depends on: stdlib (dataclasses, enum, json, typing) + agent.core.{protocol,plan} + agent.ports
Depended by: core.harness, core.report（review 字段）
ADRs: ADR-013（评审与报告契约）, ADR-014（role 路由）, ADR-015（评审关闭思考通道）,
    ADR-010（schema 约束）
"""

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping, Sequence

from agent.core.plan import Plan
from agent.core.protocol import ProtocolError, ToolResult, require_mapping, require_text
from agent.ports import BrainPort, BrainRequest


class ReviewError(ProtocolError):
    """评审结果不合法（模型输出越界或字段缺失）。

    前置条件：无。
    后置条件：异常信息指出具体字段。
    禁止事项：不要用它是表达"评审说答案不好"（那是 verdict 的职责）。
    异常来源：Review.from_payload。
    """


class Verdict(StrEnum):
    """评审结论（ADR-013）。"""

    PASS = "PASS"
    REVISE = "REVISE"
    FAIL = "FAIL"


@dataclass(frozen=True, slots=True)
class Review:
    """一次评审的结论。

    前置条件：score 已在 [0,1]；issues/suggestions 为字符串序列。
    后置条件：不可变；to_payload 可直接进 REVIEW 信封。
    禁止事项：不要把 verdict 当纯展示字段；ADR-037 起 REVISE/FAIL 会触发 harness 打回重做。
    """

    verdict: Verdict
    score: float
    issues: tuple[str, ...]
    suggestions: tuple[str, ...]

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "Review":
        """把模型返回的 JSON 校验成 Review。

        前置条件：payload 来自受 schema 约束的解码结果。
        后置条件：score 被夹到 [0,1]（FP64）；未知 verdict 抛 ReviewError。
        禁止事项：不要为缺失 issues 补空列表后当作"无问题通过"（缺失即非法）。
        异常：ReviewError（字段缺失/类型错误/枚举越界）。
        """
        data = require_mapping(payload, "review")
        verdict_text = require_text(data.get("verdict"), "review.verdict").strip().upper()
        try:
            verdict = Verdict(verdict_text)
        except ValueError as exc:
            raise ReviewError(f"review.verdict must be PASS/REVISE/FAIL, got {verdict_text!r}") from exc

        raw_score = data.get("score")
        if not isinstance(raw_score, (int, float)) or isinstance(raw_score, bool):
            raise ReviewError("review.score must be a number")
        score = min(max(float(raw_score), 0.0), 1.0)

        return cls(
            verdict=verdict,
            score=score,
            issues=_text_tuple(data.get("issues"), "review.issues"),
            suggestions=_text_tuple(data.get("suggestions"), "review.suggestions"),
        )

    def to_payload(self) -> dict[str, Any]:
        """转为 REVIEW 信封的 payload。

        前置条件：对象已通过 from_payload 构造。
        后置条件：返回可 JSON 序列化的 dict。
        禁止事项：不要把 tuple 直接塞进 payload（保持 JSON 可读性）。
        异常：无。
        """
        return {
            "verdict": str(self.verdict),
            "score": self.score,
            "issues": list(self.issues),
            "suggestions": list(self.suggestions),
        }


_REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["PASS", "REVISE", "FAIL"]},
        "score": {"type": "number"},
        "issues": {"type": "array", "maxItems": 3, "items": {"type": "string", "maxLength": 240}},
        "suggestions": {"type": "array", "maxItems": 3, "items": {"type": "string", "maxLength": 240}},
    },
    "required": ["verdict", "score", "issues", "suggestions"],
}

# 公开别名：delegate.review 复用同一份 schema，避免两处定义漂移。
REVIEW_SCHEMA = _REVIEW_SCHEMA

_REVIEW_PROMPT = """You are an independent reviewer of a local agent's work. Judge the DRAFT ANSWER
against the evidence the agent actually collected. Reply with JSON only.

Rules:
- verdict=PASS only if the answer is supported by the tool results and addresses the request.
- verdict=REVISE if fixable (wrong tool, missing evidence, unsupported claim).
- verdict=FAIL if the answer contradicts the evidence or the request was not attempted.
- score is 0..1 (0 = useless, 1 = fully supported).
- issues: at most 3 concrete defects, each under 240 characters; suggestions: at most 3, same limit.
- If the user asks for steps/process and a knowledge hit includes an "outline"/"document_outline" list, check that
  the draft covers the major sections; REVISE if it only covers a small subset.
- Never invent facts that are not in the tool results. Be terse: long prose truncates your JSON output.

User request:
{user_text}

Plan (JSON):
{plan}

Tool results (JSON):
{results}

Draft answer:
{draft}"""


class Reviewer:
    """用次脑评审一轮的产物（ADR-013）。

    前置条件：brain 指向支持 role="review" 的实现（见 ADR-014）。
    后置条件：review() 返回校验过的 Review；评审失败时抛 BrainError/ReviewError，由 harness 降级。
    禁止事项：不要在此修改 plan/answer（评审只读）。
    """

    def __init__(self, *, brain: BrainPort, temperature: float, max_tokens: int) -> None:
        self._brain = brain
        self._temperature = temperature
        self._max_tokens = max_tokens

    def review(
        self,
        *,
        user_text: str,
        plan: Plan,
        results: Sequence[ToolResult],
        draft_answer: str,
    ) -> Review:
        """对一轮产物给出评审。

        前置条件：results 可能为空；draft_answer 非空。
        后置条件：返回 Review；issue 文案不超过模型返回内容（不做改写）。
        禁止事项：不要在评审提示里注入主脑的自我评价（会污染独立性）。
        异常：BrainError（调用失败）、ReviewError（结构非法）。
        """
        evidence = _compact_evidence(results)
        prompt = _REVIEW_PROMPT.format(
            user_text=user_text,
            plan=json.dumps(plan.to_payload(), ensure_ascii=False, indent=2),
            results=json.dumps(evidence, ensure_ascii=False, indent=2),
            draft=draft_answer,
        )
        reply = self._brain.complete(
            BrainRequest(
                messages=(
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": "Review the draft answer above."},
                ),
                json_schema=_REVIEW_SCHEMA,
                temperature=self._temperature,
                max_tokens=self._max_tokens,
                role="review",
                # 评审输出只有四个字段，思考只会挤占预算（ADR-015）。
                disable_thinking=True,
            )
        )
        return Review.from_payload(reply.content)


def _text_tuple(value: Any, field_name: str) -> tuple[str, ...]:
    """把 JSON 数组校验成字符串元组。

    前置条件：value 应为 list/tuple。
    后置条件：返回去空白后的字符串元组（允许空数组）。
    禁止事项：不要静默丢弃非字符串元素（宁可报错也不吞）。
    异常：ReviewError（非数组或含非字符串元素）。
    """
    if not isinstance(value, (list, tuple)):
        raise ReviewError(f"{field_name} must be an array")
    items: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise ReviewError(f"{field_name}[{index}] must be a string")
        text = item.strip()
        if text:
            items.append(text)
    return tuple(items)


_EVIDENCE_CHARS = 600


def _compact_evidence(results: Sequence[ToolResult]) -> list[dict[str, Any]]:
    """把工具结果压成评审可读的紧凑证据（ADR-015 的裁剪路线）。

    前置条件：results 可能为空。
    后置条件：字符串值截断到 _EVIDENCE_CHARS；列表最多 10 项、映射最多 20 键。
    禁止事项：不要丢掉 ok/error（评审必须看到失败），也不要改写文本语义。
    异常：无。
    """
    return [
        {
            "call_id": result.call_id,
            "ok": result.ok,
            "data": _shrink(dict(result.data)),
            "error": None if result.error is None else result.error.to_payload(),
        }
        for result in results
    ]


def _shrink(value: Any, depth: int = 0) -> Any:
    """递归压缩证据里的长值。

    前置条件：depth <= 3（超出直接截断成字符串摘要）。
    后置条件：返回可 JSON 序列化的同类结构。
    禁止事项：不要对非容器类型做类型转换。
    异常：无。
    """
    if depth > 3:
        return "…"
    if isinstance(value, str):
        if len(value) <= _EVIDENCE_CHARS:
            return value
        return value[: _EVIDENCE_CHARS - 1] + "…"
    if isinstance(value, Mapping):
        return {str(key): _shrink(item, depth + 1) for key, item in list(value.items())[:20]}
    if isinstance(value, (list, tuple)):
        return [_shrink(item, depth + 1) for item in list(value)[:10]]
    return value
