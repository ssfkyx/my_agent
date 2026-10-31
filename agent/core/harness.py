"""harness — 单轮 Turn 的顺序编排器。

Module: agent.core.harness
Responsibility: 把一次用户输入编排成 USER_INPUT → PLAN → (TOOL_CALL/TOOL_RESULT)* → REVIEW → REFLECTION
    → ANSWER → RUN_REPORT，负责状态推进、错误转 ERROR 信封、评审/反思/报告落盘，并把每条事件交给 UiPort。
Depends on: agent.core.{protocol,turn,plan,policy,learning,reflection,report}, agent.ports, agent.tools.registry
Depended by: agent.app, agent.__main__
ADRs: ADR-001（事件形状）, ADR-003（FP64 计时）, ADR-004（BrainPort）, ADR-005（顺序流水线）,
    ADR-006（范围裁剪）, ADR-009（工具护栏）, ADR-010（计划修复重试）, ADR-011（提示契约）,
    ADR-012（token 预算覆盖思考通道）, ADR-013（评审与报告）, ADR-014（role 路由）,
    ADR-018（反思与只追加记忆）, ADR-021（失败预算、同意与升级）, ADR-022（脱敏）,
    ADR-023（角色切换与 ROLE_SWITCH）, ADR-034（RuntimeInfoPort / 报告记录 ngl）,
    ADR-037（评审门禁、证据校验、动态步数）, ADR-038（answer 结果预算）, ADR-039（失败日志/演进提案）, ADR-042（可选次脑/反思）
"""

import json
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from agent.core.budget import ProgressWatch, TurnBudget
from agent.core.escalation import Escalator
from agent.core.evolution import append_failure
from agent.core.learning import Review, Reviewer, Verdict
from agent.core.plan import Plan, Step, StepKind
from agent.core.policy import Guardrail
from agent.core.reflection import Reflection, Reflector, lessons_from
from agent.core.report import Reporter
from agent.core.protocol import (
    Envelope,
    ErrorCode,
    ErrorInfo,
    MessageKind,
    ProtocolError,
    ToolCall,
    ToolResult,
    require_text,
)
from agent.core.turn import Turn, TurnState
from agent.ports import (
    BrainError,
    BrainPort,
    BrainRequest,
    EscalationError,
    MemoryPort,
    RolePort,
    RuntimeInfoPort,
    UiPort,
)
from agent.tools.registry import ToolNotFoundError, ToolRegistry

_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "goal": {"type": "string", "maxLength": 300},
        "rationale": {"type": "string", "maxLength": 600},
        "steps": {
            "type": "array",
            "maxItems": 12,
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["tool", "answer"]},
                    "target": {"type": "string", "maxLength": 100},
                    "args": {"type": "object"},
                    "why": {"type": "string", "maxLength": 200},
                },
                "required": ["kind", "target", "why"],
            },
        },
    },
    "required": ["goal", "rationale", "steps"],
}

# ADR-037：计划最多 12 步，输出上限压到 2048，避免重规划时挤爆 n_ctx。
_PLAN_MAX_TOKENS = 2048

_ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["answer"],
}

_PLANNER_PROMPT = """You are the planner of a local agent that runs exactly one model at a time.
Reply with JSON only, matching the given schema. Rules:
- Use 1..{max_steps} steps; every step needs a short "why".
- Detect the user's language and answer in that language.
- For knowledge.search, the primary query MUST use the user's language. If the user request is Chinese,
  you MUST also plan one English query for the same concepts; if it is English, you MUST also plan one
  Chinese query. These two searches are mandatory; do not repeat equivalent queries.
- Include at least one step with kind="tool".
- End with exactly one step with kind="answer" and target="".
- For tool steps, "target" MUST be one of the available tool names, and "args" must satisfy its args_schema.
- "rationale" explains the ordering decision, not the goal.
- "workspace.echo" is a pipeline probe ONLY. Never use it to "handle" a real request:
  if the user asks to read, list or inspect a path, call "workspace.read_file" or "workspace.list_dir".
- Never simulate a tool result: do not put file contents, error messages, apologies or conclusions into
  "workspace.echo" args. If a path request may be refused, still call the matching workspace tool and let it report.
- If the request needs no file access at all, you may call "workspace.echo" once, then answer.
- All step args are fixed NOW: a step cannot reference a later step's output. So never plan a call that
  needs an id you have not seen yet (e.g. do not plan knowledge.read_section with a guessed doc_id).
  knowledge.search already returns the passages you need; plan the primary query plus at most one
  cross-language query, then answer.
- Copy identifiers (doc_id, heading, call_id) VERBATIM from earlier tool results; never invent one.
- For code/file tasks, include a verification step after writing when a matching command exists; do not
  claim success without a successful verification result.
- "delegate.switch_role" changes the role used by later model calls in this same turn. Use it ONLY when
  the user explicitly asks to switch role; it does not replan, start a new turn, or clear context.
- Use "delegate.draft" when the user asks for a draft and "delegate.review" for an independent second
  opinion. Both use the review model and switch the active role to review for later calls.
- Use "harness.propose_change" only for a concrete harness/tool/prompt improvement; it queues a
  proposal and never changes code or configuration during the current turn. A proposal must include
  current_behavior, root_cause, at least two alternatives (option/description/tradeoff), the selected
  proposal, expected_metric{{metric,baseline,target,method}}, replay_plan, risk, and rollback. Do not
  duplicate an existing rule; quote the current behavior you verified.

Lessons from previous runs (follow unless the user contradicts them):
{memory}

Available tools (JSON):
{catalog}"""

_ANSWER_PROMPT = """You are the executor of a local agent. Answer the user using ONLY the tool results below.
Reply with JSON: {{"answer": string, "confidence": number between 0 and 1}}.
Answer in the same language as the user request.
If a tool failed, state the failure and what would be needed next. Never invent data.
If the results do not contain the requested information, say plainly that it could not be obtained and why.
Never claim an action you did not perform (e.g. do not say "I am reading the file" unless a read result exists).
Cite the doc_id you actually received (e.g. from knowledge.search hits); never invent identifiers.
The search passages are already quoted evidence: answer from them directly instead of asking for a re-read.
If a result has "escalated": true, its "text" is the external master answer; use it as evidence and
mention that local attempts failed first. If escalation itself failed, say so plainly.

Plan goal: {goal}

Tool results (JSON):
{results}

User request:
{user_text}"""

# ADR-010：非法计划只修一次。可调预算属于 TB-6 的 TurnBudget，此处写死。
_PLAN_REPAIR_LIMIT = 1

# ADR-023/024：这些工具成功后都在 ledger 中留下 ROLE_SWITCH 事件。
_ROLE_SWITCH_TOOLS = frozenset(
    {"delegate.switch_role", "delegate.draft", "delegate.review"}
)

_REPAIR_PROMPT = """Your previous reply was rejected by the validator: {error}
Reply again with JSON only, matching the same schema. Keep the same user request."""

# ADR-037：回答里声称“测试通过/已验证”时，必须能在工具结果里找到成功测试。
_COMPLETION_CLAIMS = (
    re.compile(r"(?i)\ball\s+tests?\s+(?:pass|passed|succeed(?:ed)?|green)\b"),
    re.compile(r"(?i)\btests?\s+(?:pass|passed|succeed(?:ed)?)\b"),
    re.compile(r"(?i)\bsmoke\s+test\s+(?:pass|passed|succeed(?:ed)?)\b"),
    re.compile(r"(?i)\btest\s+suite\s+(?:pass|passed|succeed(?:ed)?)\b"),
    re.compile(r"(?i)\b(?:verified|verification succeeded)\b"),
    re.compile(r"测试(?:全部)?通过|测试成功|验证通过|已通过测试"),
)
_NEGATED_VERIFICATION = re.compile(
    r"(?i)(?:cannot|can't|unable to)\s+(?:confirm|verify)|not\s+verified|未验证|无法确认|未通过"
)
_TEST_COMMAND_TOKENS = ("unittest", "pytest", "test_", "tests/", " test")


def _has_successful_test_evidence(results: list[Any]) -> bool:
    """工具结果里是否有 returncode=0 的测试命令。"""
    for result in results:
        if not getattr(result, "ok", False):
            continue
        data = getattr(result, "data", None)
        if not isinstance(data, Mapping):
            continue
        if data.get("returncode") != 0:
            continue
        argv = data.get("argv")
        if not isinstance(argv, (list, tuple)):
            continue
        command = " ".join(str(item) for item in argv).casefold()
        if any(token in command for token in _TEST_COMMAND_TOKENS):
            return True
    return False


def _unsupported_completion_claim(answer_text: str, results: list[Any]) -> str | None:
    """检测“测试通过”类声明是否缺少工具证据；有则返回缺陷描述。"""
    if not any(pattern.search(answer_text) for pattern in _COMPLETION_CLAIMS):
        return None
    if _NEGATED_VERIFICATION.search(answer_text):
        return None
    if _has_successful_test_evidence(results):
        return None
    return (
        "answer claims tests/verification passed, but no successful test command "
        "appears in the tool results"
    )


def _merge_review_issue(review: Review | None, issue: str) -> Review:
    """把证据校验缺陷并入评审结论，强制至少 REVISE。"""
    issues = list(review.issues) if review is not None else []
    if issue not in issues:
        issues.append(issue)
    suggestions = list(review.suggestions) if review is not None else []
    if not suggestions:
        suggestions = ["Run the verification command and report its actual return code/output."]
    verdict = Verdict.FAIL if review is not None and review.verdict is Verdict.FAIL else Verdict.REVISE
    score = min(review.score if review is not None else 0.0, 0.5)
    return Review(
        verdict=verdict,
        score=score,
        issues=tuple(issues[:3]),
        suggestions=tuple(suggestions[:3]),
    )


def _review_feedback(review: Review, results: list[Any]) -> str:
    """把评审结论 + 最近工具失败压成给 planner 的重做提示。"""
    lines = [f"Review verdict: {review.verdict}", "Issues:"]
    lines.extend(f"- {issue}" for issue in review.issues)
    if review.suggestions:
        lines.append("Suggestions:")
        lines.extend(f"- {suggestion}" for suggestion in review.suggestions)

    failures: list[str] = []
    for result in results[-8:]:
        if not getattr(result, "ok", False):
            error = getattr(result, "error", None)
            code = getattr(error, "code", "ERROR")
            message = getattr(error, "message", "tool failed")
            failures.append(f"- {result.call_id}: {code}: {message}")
            continue
        data = getattr(result, "data", None)
        if isinstance(data, Mapping) and data.get("returncode") not in (None, 0):
            argv = " ".join(str(item) for item in data.get("argv", []))
            stderr = str(data.get("stderr", ""))[:200]
            failures.append(
                f"- {result.call_id}: command `{argv}` returncode={data.get('returncode')} stderr={stderr}"
            )
    if failures:
        lines.append("Recent tool failures:")
        lines.extend(failures[-5:])
    return "\n".join(lines)[:2400]


def _is_cjk(text: str) -> bool:
    """粗判文本是否以中文为主；只用于回退文案。"""
    return any("\u4e00" <= char <= "\u9fff" for char in text)


def _constrained_answer(
    draft: str,
    review: Review | None,
    error: str,
    results: list[Any],
    user_text: str,
) -> str:
    """重规划失败时的受限答案：保留草稿，明确标记未完成。"""
    issues = "；".join(review.issues[:2]) if review is not None else "评审要求修正"
    evidence = sum(1 for result in results if getattr(result, "ok", False))
    if _is_cjk(user_text):
        lines = [
            draft.strip() or "这次请求没有完成。",
            "",
            "未完成说明：评审要求修正，但重做计划失败，未能完成修正。",
            f"评审意见：{issues}",
            f"失败原因：{error[:300]}",
            f"已有工具结果：{evidence} 条；以上内容仅基于现有证据，尚未完成验证。",
        ]
    else:
        lines = [
            draft.strip() or "This request could not be completed.",
            "",
            "Incomplete: the reviewer requested a revision, but replanning failed.",
            f"Review issues: {issues}",
            f"Replan error: {error[:300]}",
            f"Tool evidence: {evidence} result(s); the text above is unverified.",
        ]
    return "\n".join(lines)


_ANSWER_CONTEXT_CHARS = 8000
_ANSWER_OTHER_CHARS = 2000


def _compact_answer_payload(results: list[ToolResult]) -> list[dict[str, Any]]:
    """压缩 answer prompt 的工具结果：检索命中按 doc 去重并限制总字符数。"""
    hits: list[dict[str, Any]] = []
    seen_docs: set[str] = set()
    other: list[dict[str, Any]] = []
    for result in results:
        if not result.ok:
            other.append(
                {
                    "tool": result.call_id,
                    "ok": False,
                    "data": {},
                    "error": None if result.error is None else result.error.to_payload(),
                }
            )
            continue
        data = result.data
        raw_hits = data.get("hits")
        if isinstance(raw_hits, list):
            for raw_hit in raw_hits:
                if not isinstance(raw_hit, Mapping):
                    continue
                doc_id = str(raw_hit.get("doc_id", ""))
                heading = str(raw_hit.get("heading", ""))
                key = f"{doc_id}#{heading}" if doc_id else heading
                if key in seen_docs:
                    continue
                seen_docs.add(key)
                hits.append(dict(raw_hit))
            continue

        proposal = data.get("proposal")
        if isinstance(proposal, Mapping):
            other.append(
                {
                    "tool": result.call_id,
                    "ok": True,
                    "data": dict(data),
                    "error": None,
                }
            )
            continue

        rendered = json.dumps(dict(data), ensure_ascii=False)
        if len(rendered) > _ANSWER_OTHER_CHARS:
            rendered = rendered[:_ANSWER_OTHER_CHARS] + "…"
        other.append(
            {
                "tool": result.call_id,
                "ok": True,
                "data": {"truncated": True, "preview": rendered},
                "error": None,
            }
        )

    compact_hits: list[dict[str, Any]] = []
    used = 0
    for hit in hits:
        if used >= _ANSWER_CONTEXT_CHARS:
            break
        text = str(hit.get("text", ""))
        room = _ANSWER_CONTEXT_CHARS - used
        if len(text) > room:
            text = text[: max(0, room - 1)] + "…"
        hit["text"] = text
        used += len(text)
        compact_hits.append(hit)

    payload: list[dict[str, Any]] = []
    if compact_hits:
        payload.append(
            {
                "tool": "knowledge.search",
                "ok": True,
                "data": {"hits": compact_hits},
                "error": None,
            }
        )
    payload.extend(other)
    return payload


@dataclass(frozen=True, slots=True)
class HarnessPolicy:
    """harness 的运行参数（来自 config）。

    前置条件：max_steps >= 1；0 <= temperature <= 2；max_tokens >= 1。
    后置条件：不可变，可在多个 turn 间共享。
    禁止事项：禁止在运行期修改（要改就换一个 policy 实例）。
    """

    max_steps: int
    temperature: float
    max_tokens: int
    max_attempts: int = 3
    escalation_rounds: int = 3
    # ADR-037：评审打回次数与每次追加的工具步数。
    max_review_repairs: int = 2
    repair_step_bonus: int = 4

    def __post_init__(self) -> None:
        if self.max_steps < 1:
            raise ValueError("max_steps must be >= 1")
        if not 0.0 <= self.temperature <= 2.0:
            raise ValueError("temperature must be within [0, 2]")
        if self.max_tokens < 1:
            raise ValueError("max_tokens must be >= 1")
        if self.max_review_repairs < 0:
            raise ValueError("max_review_repairs must be >= 0")
        if self.repair_step_bonus < 0:
            raise ValueError("repair_step_bonus must be >= 0")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.escalation_rounds < 1:
            raise ValueError("escalation_rounds must be >= 1")


@dataclass(frozen=True, slots=True)
class TurnOutcome:
    """一个 turn 的结果与审计位置。

    前置条件：由 Harness.run_turn 构造。
    后置条件：ledger_path 指向已落盘的 JSONL；answer.kind 为 ANSWER 或 ERROR；
        review 为 None 表示评审不可用；report_* 为两份报告路径。
    禁止事项：不要把 outcome 当作可变状态继续改写。
    """

    answer: Envelope
    ledger_path: Path
    state: TurnState
    review: Review | None = None
    report_json: Path | None = None
    report_md: Path | None = None


@dataclass(frozen=True, slots=True)
class StepOutcome:
    """一个计划步骤执行结束后的内部结果。

    前置条件：由 Harness._execute_step 构造。
    后置条件：result 为最后一条 ToolResult；errors 为本次步骤的全部失败；
        needs_consent=True 表示预算已耗尽、需要询问用户是否升级。
    禁止事项：不要把 StepOutcome 写进 ledger（ledger 只收 Envelope）。
    """

    result: ToolResult
    errors: tuple[ErrorInfo, ...] = ()
    needs_consent: bool = False


@dataclass(frozen=True, slots=True)
class EscalationOutcome:
    """一次升级处理的结果：要么中止本轮，要么得到一条额外证据。

    前置条件：由 Harness._escalate 构造；abort 与 evidence 不同时为 None。
    后置条件：abort 非空时调用方必须立即返回它；evidence 非空时加入答案证据。
    禁止事项：不要在 abort 之后继续调用模型。
    """

    abort: TurnOutcome | None = None
    evidence: ToolResult | None = None


class Harness:
    """单轮编排器（ADR-005）。

    前置条件：brain/tools/ui/guardrail/reviewer/reporter 均已就绪；ledger_dir 可写。
    后置条件：run_turn 返回时，事件已渲染、报告已落盘、模型调用已全部结束。
    禁止事项：禁止并行调用 run_turn（顺序流水线，且显存只容一个模型）。
    """

    def __init__(
        self,
        *,
        brain: BrainPort,
        tools: ToolRegistry,
        ui: UiPort,
        policy: HarnessPolicy,
        guardrail: Guardrail,
        reviewer: Reviewer | None,
        reporter: Reporter,
        reflector: Reflector | None,
        memory: MemoryPort,
        recall_limit: int,
        ledger_dir: Path,
        escalator: Escalator | None = None,
        roles: RolePort | None = None,
        runtime: RuntimeInfoPort | None = None,
    ) -> None:
        self._brain = brain
        self._tools = tools
        self._ui = ui
        self._policy = policy
        self._guardrail = guardrail
        self._reviewer = reviewer
        self._reflector = reflector
        self._memory = memory
        self._recall_limit = recall_limit
        self._reporter = reporter
        self._ledger_dir = ledger_dir
        self._escalator = escalator
        self._roles = roles
        self._runtime = runtime
        self._progress = ProgressWatch(escalation_rounds=policy.escalation_rounds)

    def run_turn(self, user_text: str, *, cancel: threading.Event | None = None) -> TurnOutcome:
        """执行一轮完整流程。

        前置条件：user_text 非空；模型已由调用方 ensure（本方法不负责拉起进程）。
        后置条件：返回 TurnOutcome；ledger 已写入 ledger_dir/<turn_id>.jsonl。
        禁止事项：不要在内部重试模型调用（重试属于未来的预算层）；
            不要在工具失败时中断整个 turn（失败要变成数据）。
        异常：ValueError（user_text 全空白）、OSError（ledger 无法写入）。
        """
        require_text(user_text, "user_text")
        if self._runtime is not None:
            self._runtime.begin_turn()
        turn_started = time.perf_counter()
        turn = Turn.open(user_text)
        self._emit(turn, MessageKind.USER_INPUT, "user", {"text": user_text})
        if (abort := self._cancel_outcome(turn, cancel)) is not None:
            return abort

        turn.state = TurnState.PLANNING
        plan_started = time.perf_counter()
        try:
            plan, lessons_used = self._plan(turn)
        except (BrainError, ProtocolError) as exc:
            return self._fail(turn, ErrorCode.PLAN_INVALID, f"planning failed: {exc}")
        if (abort := self._cancel_outcome(turn, cancel)) is not None:
            return abort

        # ADR-037：评审可以打回重做；每轮追加 repair_step_bonus，但总步数封顶。
        total_step_limit = self._policy.max_steps + (
            self._policy.max_review_repairs * self._policy.repair_step_bonus
        )
        executed_total = 0
        all_results: list[ToolResult] = []
        review: Review | None = None
        repair_round = 0
        feedback = ""
        answer_text = ""
        confidence = 0.0
        answer_elapsed = 0.0

        while True:
            round_limit = min(
                self._policy.max_steps + repair_round * self._policy.repair_step_bonus,
                total_step_limit,
            )
            self._emit(
                turn,
                MessageKind.PLAN,
                "harness",
                plan.to_payload(),
                elapsed_s=time.perf_counter() - plan_started,
                lessons=lessons_used,
                repair_round=repair_round,
                review_feedback=feedback,
            )

            # ProgressWatch：同一“意图 + 工具序列”反复出现时，为升级询问提供旁证。
            fingerprint = ProgressWatch.fingerprint(
                plan.goal,
                [step.target for step in plan.steps if step.kind is StepKind.TOOL],
            )
            plan_rounds = self._progress.note(fingerprint)

            turn.state = TurnState.ACTING
            round_results: list[ToolResult] = []
            executed_round = 0
            for step in plan.steps:
                if (abort := self._cancel_outcome(turn, cancel)) is not None:
                    return abort
                if step.kind is StepKind.ANSWER:
                    break
                if executed_round >= round_limit or executed_total >= total_step_limit:
                    self._emit(
                        turn,
                        MessageKind.ERROR,
                        "harness",
                        {
                            "code": str(ErrorCode.PLAN_INVALID),
                            "message": (
                                f"plan exceeded step budget (round_limit={round_limit}, "
                                f"total_limit={total_step_limit}); remaining steps skipped"
                            ),
                        },
                    )
                    break
                step_outcome = self._execute_step(turn, step)
                round_results.append(step_outcome.result)
                all_results.append(step_outcome.result)
                executed_round += 1
                executed_total += 1
                if step_outcome.needs_consent:
                    escalation = self._escalate(turn, plan, step, step_outcome.errors, plan_rounds)
                    if escalation.abort is not None:
                        return escalation.abort
                    if escalation.evidence is not None:
                        round_results.append(escalation.evidence)
                        all_results.append(escalation.evidence)
                    # 升级已给出外部答案，剩余本地步骤不再执行，直接进入作答。
                    break

            turn.state = TurnState.ANSWERING
            if (abort := self._cancel_outcome(turn, cancel)) is not None:
                return abort
            answer_started = time.perf_counter()
            answer_text, confidence = self._answer(turn, plan, all_results)
            answer_elapsed = time.perf_counter() - answer_started
            if (abort := self._cancel_outcome(turn, cancel)) is not None:
                return abort

            # 先评审草稿；REVISE/FAIL 时把 issues 传回 planner 重做（ADR-013/037）。
            review = self._review(turn, plan, all_results, answer_text)
            evidence_issue = _unsupported_completion_claim(answer_text, all_results)
            if evidence_issue:
                review = _merge_review_issue(review, evidence_issue)
                self._emit(
                    turn,
                    MessageKind.REVIEW,
                    "harness",
                    review.to_payload(),
                    gate="evidence",
                    issue=evidence_issue,
                )
            if (abort := self._cancel_outcome(turn, cancel)) is not None:
                return abort

            if review is None or review.verdict is Verdict.PASS:
                break
            if repair_round >= self._policy.max_review_repairs:
                break

            repair_round += 1
            feedback = _review_feedback(review, all_results)
            self._emit(
                turn,
                MessageKind.RETRY,
                "harness",
                {
                    "stage": "review",
                    "attempt": repair_round,
                    "limit": self._policy.max_review_repairs,
                    "reason": evidence_issue or feedback,
                },
            )
            turn.state = TurnState.PLANNING
            plan_started = time.perf_counter()
            try:
                plan, lessons_used = self._plan(
                    turn,
                    step_limit=min(
                        round_limit + self._policy.repair_step_bonus,
                        total_step_limit,
                    ),
                    feedback=feedback,
                )
            except (BrainError, ProtocolError) as exc:
                # ADR-037：重规划失败不再丢弃整轮；退回基于已有证据的受限答案。
                self._emit(
                    turn,
                    MessageKind.ERROR,
                    "harness",
                    {
                        "code": str(ErrorCode.PLAN_INVALID),
                        "message": f"replan failed: {exc}; delivering constrained answer",
                    },
                )
                answer_text = _constrained_answer(
                    answer_text,
                    review,
                    str(exc),
                    all_results,
                    turn.user_text,
                )
                confidence = min(confidence, 0.5)
                turn.state = TurnState.ANSWERING
                break
            if (abort := self._cancel_outcome(turn, cancel)) is not None:
                return abort

        self._reflect(turn, plan, all_results, answer_text, review)
        answer = self._emit(
            turn,
            MessageKind.ANSWER,
            "agent",
            {"text": answer_text, "confidence": confidence, "goal": plan.goal},
            elapsed_s=answer_elapsed,
        )

        report = self._reporter.build(
            turn=turn,
            plan=plan,
            review=review,
            answer=answer,
            duration_s=time.perf_counter() - turn_started,
            ledger_path=self._ledger_dir / f"{turn.turn_id}.jsonl",
            models=self._runtime.snapshot() if self._runtime is not None else None,
        )
        report_json, report_md = self._reporter.write(report, run_dir=self._ledger_dir)
        self._emit(
            turn,
            MessageKind.RUN_REPORT,
            "harness",
            dict(report.to_payload()),
            report_json=str(report_json),
            report_md=str(report_md),
        )
        turn.state = TurnState.DELIVERED

        ledger_path = turn.ledger.save(self._ledger_dir / f"{turn.turn_id}.jsonl")
        self._ui.report(answer, ledger_path)
        return TurnOutcome(
            answer=answer,
            ledger_path=ledger_path,
            state=turn.state,
            review=review,
            report_json=report_json,
            report_md=report_md,
        )

    def _plan(
        self,
        turn: Turn,
        *,
        step_limit: int | None = None,
        feedback: str = "",
    ) -> tuple[Plan, int]:
        """向主脑要一份计划并校验（非法时做 1 次修复重试，ADR-010）。

        前置条件：turn 处于 PLANNING；step_limit 为 None 或 >=1；feedback 可为空。
        后置条件：返回结构合法的 Plan（steps 非空）；重试时 ledger 含 RETRY 事件。
        禁止事项：不要在这里替模型补 steps 或改写 target；不要重试第二次。
        异常：BrainError（调用失败）、PlanError（两次都非法）。
        """
        limit = self._policy.max_steps if step_limit is None else step_limit
        if limit < 1:
            raise ValueError("step_limit must be >= 1")
        catalog = [spec.to_payload() for spec in self._tools.catalog()]
        lessons = self._lessons()
        memory_block = "\n".join(f"- {lesson}" for lesson in lessons) if lessons else "none yet"
        system = _PLANNER_PROMPT.format(
            max_steps=limit,
            memory=memory_block,
            catalog=json.dumps(catalog, ensure_ascii=False, indent=2),
        )
        messages: list[Mapping[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": turn.user_text},
        ]
        if feedback:
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "The previous attempt was rejected by the reviewer. "
                        "Produce a NEW plan that fixes these issues; do not repeat the failed work:\n"
                        + feedback
                    ),
                }
            )
        reply = self._call_planner(messages, max_tokens=_PLAN_MAX_TOKENS)
        try:
            return Plan.from_payload(reply.content), len(lessons)
        except ProtocolError as exc:
            self._emit(
                turn,
                MessageKind.RETRY,
                "harness",
                {
                    "stage": "plan",
                    "attempt": 1,
                    "limit": _PLAN_REPAIR_LIMIT,
                    "reason": str(exc),
                },
            )
            messages.extend(
                [
                    {"role": "assistant", "content": reply.raw},
                    {"role": "user", "content": _REPAIR_PROMPT.format(error=exc)},
                ]
            )
            repaired = self._call_planner(messages, max_tokens=_PLAN_MAX_TOKENS)
            return Plan.from_payload(repaired.content), len(lessons)

    def _active_role(self) -> str:
        """返回当前主脑角色名。

        前置条件：roles 可为 None（旧测试/单角色场景）。
        后置条件：有 RolePort 时返回其 current；否则回落 main。
        禁止事项：不要在评审/反思中使用它（它们固定 role=review，ADR-014）。
        异常：无。
        """
        return self._roles.current if self._roles is not None else "main"

    def _call_planner(
        self,
        messages: list[Mapping[str, Any]],
        *,
        max_tokens: int | None = None,
    ) -> Any:
        """以计划 schema 调用一次主脑。

        前置条件：messages 至少含 system 与 user 各一条；max_tokens 可覆盖默认值。
        后置条件：返回 BrainReply；content 为受 schema 约束的 dict。
        禁止事项：不要把 messages 改成自由文本（会绕过约束解码）。
        异常：BrainError（网络/解析失败）。
        """
        limit = min(self._policy.max_tokens, max_tokens) if max_tokens is not None else self._policy.max_tokens
        return self._brain.complete(
            BrainRequest(
                messages=tuple(messages),
                json_schema=_PLAN_SCHEMA,
                temperature=self._policy.temperature,
                max_tokens=limit,
                role=self._active_role(),
            )
        )

    def _lessons(self) -> tuple[str, ...]:
        """读取最近记忆中的 lessons（注入计划提示）。

        前置条件：recall_limit >= 0。
        后置条件：返回至多 recall_limit 条短句；记忆不可读时返回空（不阻断计划）。
        禁止事项：不要因为记忆损坏而中止 turn（记忆是 best-effort）。
        异常：无（OSError 被吞掉）。
        """
        try:
            entries = self._memory.recent(self._recall_limit)
        except OSError:
            return ()
        return lessons_from(entries, self._recall_limit)

    def _reflect(
        self,
        turn: Turn,
        plan: Plan,
        results: list[ToolResult],
        draft_answer: str,
        review: Review | None,
    ) -> Reflection | None:
        """生成并落盘一条反思（ADR-018，best-effort）。

        前置条件：评审已结束（review 可为 None）。
        后置条件：成功时 ledger 有 REFLECTION 事件且 memory 多一行；失败返回 None 且不记 ERROR。
        禁止事项：不要因反思失败而影响答案交付；不要回写历史记忆。
        异常：无（BrainError/ProtocolError/OSError 均被降级）。
        """
        if self._reflector is None:
            return None
        started = time.perf_counter()
        try:
            reflection = self._reflector.reflect(
                turn_id=turn.turn_id,
                user_text=turn.user_text,
                plan=plan,
                results=results,
                draft_answer=draft_answer,
                review=review,
            )
            self._memory.append(reflection.to_payload())
        except (BrainError, ProtocolError, OSError):
            return None
        self._emit(
            turn,
            MessageKind.REFLECTION,
            "reviewer",
            reflection.to_payload(),
            elapsed_s=time.perf_counter() - started,
        )
        return reflection

    def _review(self, turn: Turn, plan: Plan, results: list[ToolResult], draft_answer: str) -> Review | None:
        """让次脑评审草稿答案；未配置次脑时返回 None（ADR-042）。

        前置条件：草稿答案已生成；brain 支持 role="review"。
        后置条件：成功时返回 Review 并落 REVIEW 信封；失败时返回 None 且落 ERROR(UPSTREAM_ERROR)。
        禁止事项：不要因评审失败而丢弃答案；不要重试评审（换模型代价高，留给预算层）。
        异常：无（评审错误被转成事件）。
        """
        if self._reviewer is None:
            return None
        started = time.perf_counter()
        try:
            review = self._reviewer.review(
                user_text=turn.user_text,
                plan=plan,
                results=results,
                draft_answer=draft_answer,
            )
        except (BrainError, ProtocolError) as exc:
            self._emit(
                turn,
                MessageKind.ERROR,
                "harness",
                {
                    "code": str(ErrorCode.UPSTREAM_ERROR),
                    "message": f"review unavailable: {exc}",
                },
            )
            return None
        self._emit(
            turn,
            MessageKind.REVIEW,
            "reviewer",
            review.to_payload(),
            elapsed_s=time.perf_counter() - started,
        )
        return review

    def _execute_step(self, turn: Turn, step: Step) -> StepOutcome:
        """执行单个工具步骤，并按失败预算决定是否重试（ADR-021）。

        前置条件：step.kind 为 TOOL；step.target 已通过 Plan 校验（非空）。
        后置条件：每次尝试都记一对 TOOL_CALL/TOOL_RESULT；可重试错误最多尝试
            max_attempts 次（含首次）；预算耗尽时 needs_consent=True。
        禁止事项：不要重试不可重试错误；不要让工具异常穿透到 run_turn。
        异常：无（工具抛出的异常被转成 INTERNAL 结果）。
        """
        budget = TurnBudget(max_attempts=self._policy.max_attempts)
        errors: list[ErrorInfo] = []
        attempt = 0
        while True:
            attempt += 1
            call = ToolCall(call_id=f"c-{turn.ledger.next_seq:04d}", tool=step.target, args=step.args)
            self._emit(
                turn,
                MessageKind.TOOL_CALL,
                "harness",
                call.to_payload(),
                step_id=step.step_id,
                why=step.why,
                attempt=attempt,
            )
            result = self._invoke_tool(call)
            self._emit(
                turn,
                MessageKind.TOOL_RESULT,
                "harness",
                result.to_payload(),
                step_id=step.step_id,
                attempt=attempt,
            )
            if result.ok:
                self._emit_role_switch(turn, step, result)
                return StepOutcome(result=result, errors=tuple(errors))

            error = result.error
            if error is None:  # 协议保证不会发生；防御性保留，避免 None 穿透。
                return StepOutcome(result=result, errors=tuple(errors))
            errors.append(error)

            # 确定性失败重试无意义，也不消耗预算（ADR-009/021）。
            if not error.retryable:
                return StepOutcome(result=result, errors=tuple(errors))

            if not budget.record_failure(error):
                return StepOutcome(result=result, errors=tuple(errors), needs_consent=True)

            self._emit(
                turn,
                MessageKind.RETRY,
                "harness",
                {
                    "stage": "tool",
                    "attempt": attempt,
                    "limit": self._policy.max_attempts,
                    "reason": f"{error.code}: {error.message}",
                },
                step_id=step.step_id,
            )

    def _invoke_tool(self, call: ToolCall) -> ToolResult:
        """解析、护栏检查并执行一次工具调用。

        前置条件：call.call_id 已由 ledger 分配。
        后置条件：返回 ToolResult；未知工具/护栏拒绝/工具崩溃都降级为数据。
        禁止事项：不要把护栏检查放到重试循环之外（每次尝试都必须重新检查参数）。
        异常：无。
        """
        started = time.perf_counter()
        try:
            tool = self._tools.resolve(call.tool)
        except ToolNotFoundError:
            return ToolResult(
                call_id=call.call_id,
                ok=False,
                error=ErrorInfo(
                    code=ErrorCode.NOT_FOUND,
                    message=f"unknown tool: {call.tool}",
                    hint="pick a target from the tool catalog",
                ),
                elapsed_s=time.perf_counter() - started,
            )

        decision = self._guardrail.check_tool(call, tool.spec)
        if not decision.success:
            # 越权/非法参数一律作为数据回执，不执行工具（ADR-009）。
            return ToolResult(
                call_id=call.call_id,
                ok=False,
                error=decision.error,
                elapsed_s=time.perf_counter() - started,
            )

        guarded_call = ToolCall(call_id=call.call_id, tool=call.tool, args=decision.value)
        try:
            return tool.invoke(guarded_call)
        # 工具实现属于可替换边界：其崩溃必须降级为数据，而不是终止审计流水。
        except Exception as exc:  # noqa: BLE001
            return ToolResult(
                call_id=call.call_id,
                ok=False,
                error=ErrorInfo(
                    code=ErrorCode.INTERNAL,
                    message=f"tool crashed: {type(exc).__name__}: {exc}",
                ),
                elapsed_s=time.perf_counter() - started,
            )

    def _emit_role_switch(self, turn: Turn, step: Step, result: ToolResult) -> None:
        """在成功切换角色后追加 ROLE_SWITCH 事件（ADR-023/024）。

        前置条件：result.ok 为 True；step.target 可能不是角色工具。
        后置条件：仅当 target ∈ {switch_role, draft, review} 时追加事件；payload 含 from/to/alias/changed。
        禁止事项：不要在切换失败时伪造 ROLE_SWITCH；不要在事件里放模型回复正文。
        异常：无。
        """
        if step.target not in _ROLE_SWITCH_TOOLS:
            return
        payload = {
            "from": result.data.get("from"),
            "to": result.data.get("to"),
            "alias": result.data.get("alias"),
            "changed": bool(result.data.get("changed")),
        }
        self._emit(
            turn,
            MessageKind.ROLE_SWITCH,
            "harness",
            payload,
            step_id=step.step_id,
            role=payload["to"],
        )

    def _escalate(
        self,
        turn: Turn,
        plan: Plan,
        step: Step,
        errors: tuple[ErrorInfo, ...],
        plan_rounds: int,
    ) -> EscalationOutcome:
        """预算耗尽后询问用户，并在同意后执行脱敏升级（ADR-021/022）。

        前置条件：errors 非空；step 为刚刚失败的步骤。
        后置条件：拒绝时返回 abort（ERROR/ABORTED，状态 FAILED，不再调用模型）；
            同意时返回一条 TOOL_RESULT 形状的 evidence；升级失败则返回 ok=False 的 evidence
            并记录 ERROR(UPSTREAM_ERROR)，答案仍照常交付。
        禁止事项：不要在未同意时外发；不要把未脱敏 payload 交给 UI 或 MasterPort。
        异常：无（升级错误被转成数据）。
        """
        if self._escalator is None:
            message = "escalation unavailable: no master configured"
            call = ToolCall(
                call_id=f"c-{turn.ledger.next_seq:04d}",
                tool="escalate.handle_to_master",
                args={"reason": message},
            )
            self._emit(turn, MessageKind.TOOL_CALL, "harness", call.to_payload(), step_id=step.step_id)
            result = ToolResult(
                call_id=call.call_id,
                ok=False,
                error=ErrorInfo(code=ErrorCode.UPSTREAM_ERROR, message=message),
                elapsed_s=0.0,
            )
            self._emit(turn, MessageKind.TOOL_RESULT, "harness", result.to_payload(), step_id=step.step_id)
            self._emit(
                turn,
                MessageKind.ERROR,
                "harness",
                {"code": str(ErrorCode.UPSTREAM_ERROR), "message": message},
            )
            return EscalationOutcome(evidence=result)

        payload = self._escalator.build_payload(
            user_text=turn.user_text,
            goal=plan.goal,
            tool=step.target,
            attempts=len(errors),
            max_attempts=self._policy.max_attempts,
            errors=errors,
        )
        preview = self._escalator.preview(payload)
        question = self._consent_question(step, errors, preview, plan_rounds)
        allowed = self._ui.ask_consent(question)
        self._emit(
            turn,
            MessageKind.CONSENT_REQUEST,
            "harness",
            {
                "question": question,
                "tool": step.target,
                "attempts": len(errors),
                "limit": self._policy.max_attempts,
                "preview": preview,
                "decision": "allow" if allowed else "deny",
            },
            step_id=step.step_id,
        )
        if not allowed:
            return EscalationOutcome(
                abort=self._fail(
                    turn,
                    ErrorCode.ABORTED,
                    f"escalation denied by user after {len(errors)} failed attempt(s); turn aborted",
                )
            )

        call = ToolCall(
            call_id=f"c-{turn.ledger.next_seq:04d}",
            tool="escalate.handle_to_master",
            args={
                "reason": f"{len(errors)} failed attempt(s) of {step.target}",
                "attempts": len(errors),
                "preview": preview,
            },
        )
        self._emit(turn, MessageKind.TOOL_CALL, "harness", call.to_payload(), step_id=step.step_id)
        started = time.perf_counter()
        try:
            text = self._escalator.escalate(payload)
        except (EscalationError, OSError) as exc:
            elapsed_s = time.perf_counter() - started
            message = f"escalation failed: {exc}"
            result = ToolResult(
                call_id=call.call_id,
                ok=False,
                error=ErrorInfo(code=ErrorCode.UPSTREAM_ERROR, message=message),
                elapsed_s=elapsed_s,
            )
            self._emit(turn, MessageKind.TOOL_RESULT, "harness", result.to_payload(), step_id=step.step_id)
            self._emit(
                turn,
                MessageKind.ERROR,
                "harness",
                {"code": str(ErrorCode.UPSTREAM_ERROR), "message": message},
            )
            return EscalationOutcome(evidence=result)

        elapsed_s = time.perf_counter() - started
        result = ToolResult(
            call_id=call.call_id,
            ok=True,
            data={
                "escalated": True,
                "text": text,
                "model": "handle_to_master",
                "attempts": len(errors),
            },
            elapsed_s=elapsed_s,
        )
        self._emit(turn, MessageKind.TOOL_RESULT, "harness", result.to_payload(), step_id=step.step_id)
        return EscalationOutcome(evidence=result)

    def _consent_question(
        self,
        step: Step,
        errors: tuple[ErrorInfo, ...],
        preview: Mapping[str, Any],
        plan_rounds: int,
    ) -> str:
        """构造面向用户的同意问题（含脱敏预览）。

        前置条件：errors 非空；preview 已脱敏。
        后置条件：返回多行文本；UI 可原样展示。
        禁止事项：不要在问题里放入未脱敏的 args/data。
        异常：无。
        """
        last = errors[-1]
        # 问题里也只用脱敏后的错误文本：本地 UI 不必看到绝对路径/邮箱。
        redacted_errors = preview.get("errors")
        last_message = last.message
        if isinstance(redacted_errors, list) and redacted_errors:
            last_entry = redacted_errors[-1]
            if isinstance(last_entry, Mapping):
                last_message = str(last_entry.get("message", last.message))
        lines = [
            f"Escalate to external master? tool '{step.target}' failed {len(errors)} time(s) "
            f"(limit {self._policy.max_attempts}).",
            f"last error: {last.code}: {last_message}",
        ]
        if plan_rounds > 1:
            lines.append(f"same plan fingerprint repeated {plan_rounds} round(s).")
        lines.append("redacted payload preview:")
        lines.append(json.dumps(dict(preview), ensure_ascii=False, indent=2))
        return "\n".join(lines)

    def _answer(self, turn: Turn, plan: Plan, results: list[ToolResult]) -> tuple[str, float]:
        """基于工具结果生成最终答复。

        前置条件：results 可能为空（计划里没有工具步骤）。
        后置条件：返回 (text, confidence)，其中 confidence 为 FP64 且落在 [0, 1]。
        禁止事项：不要在没有工具结果时编造事实；失败路径必须回落到确定性摘要。
        异常：无（模型或解析失败时走 _fallback_answer）。
        """
        payload = _compact_answer_payload(results)
        prompt = _ANSWER_PROMPT.format(
            goal=plan.goal,
            results=json.dumps(payload, ensure_ascii=False, indent=2),
            user_text=turn.user_text,
        )
        try:
            reply = self._brain.complete(
                BrainRequest(
                    messages=(
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": turn.user_text},
                    ),
                    json_schema=_ANSWER_SCHEMA,
                    temperature=self._policy.temperature,
                    max_tokens=self._policy.max_tokens,
                    role=self._active_role(),
                )
            )
            text = require_text(reply.content.get("answer"), "answer")
            confidence = float(reply.content.get("confidence", 0.0))
        except (BrainError, ProtocolError, TypeError, ValueError):
            return self._fallback_answer(results)
        return text, min(max(confidence, 0.0), 1.0)

    def _fallback_answer(self, results: list[ToolResult]) -> tuple[str, float]:
        """模型不可用时的确定性摘要（保证"错误也是数据"）。

        前置条件：results 可能为空。
        后置条件：返回可读文本与 confidence=0.0。
        禁止事项：不要在此伪造模型结论或给出建议性事实。
        异常：无。
        """
        if not results:
            return ("Model call failed and no tool result is available.", 0.0)
        payload = _compact_answer_payload(results)
        rendered = json.dumps(payload, ensure_ascii=False)
        return ("Model call failed. Compact tool output:\n" + rendered[:4000], 0.0)

    def _emit(
        self,
        turn: Turn,
        kind: MessageKind,
        sender: str,
        payload: Mapping[str, Any],
        **meta: Any,
    ) -> Envelope:
        """追加事件到 ledger 并交给 UI 渲染。

        前置条件：kind 与 payload 合法。
        后置条件：返回已入账的 Envelope（seq 已分配）。
        禁止事项：不要绕过本方法直接 append（否则 UI 会漏渲染）。
        异常：ProtocolError（payload 非法）。
        """
        envelope = turn.ledger.append(kind, sender, payload, **meta)
        if kind is MessageKind.ERROR:
            try:
                append_failure(
                    self._ledger_dir / "evolution" / "failures.jsonl",
                    {
                        "turn_id": turn.turn_id,
                        "user_text": turn.user_text,
                        "code": str(payload.get("code", "ERROR")),
                        "message": str(payload.get("message", ""))[:2000],
                        "state": str(turn.state),
                        "ledger": str(self._ledger_dir / f"{turn.turn_id}.jsonl"),
                    },
                )
            except OSError:
                pass
        self._ui.render(envelope)
        return envelope

    def _cancel_outcome(self, turn: Turn, cancel: threading.Event | None) -> TurnOutcome | None:
        """在协作检查点把取消收敛成 ABORTED。

        前置条件：turn 已开启。
        后置条件：cancel 未设置时返回 None；已设置时返回 ERROR(ABORTED) 并 report。
        禁止事项：不要在这里中断工具/模型调用（调用方只能在调用返回后检查）。
        异常：OSError（ledger 无法写入）。
        """
        if cancel is None or not cancel.is_set():
            return None
        return self._fail(turn, ErrorCode.ABORTED, "cancelled by user")

    def _fail(self, turn: Turn, code: ErrorCode, message: str) -> TurnOutcome:
        """把失败收敛成 ERROR 信封并落盘。

        前置条件：turn 已开启。
        后置条件：返回 outcome，state=FAILED，ledger 已保存，UI 已 report。
        禁止事项：不要让异常继续冒泡（CLI 需要稳定的退出码与审计文件）。
        异常：OSError（ledger 无法写入）。
        """
        turn.state = TurnState.FAILED
        envelope = self._emit(
            turn,
            MessageKind.ERROR,
            "harness",
            {"code": str(code), "message": message},
        )
        ledger_path = turn.ledger.save(self._ledger_dir / f"{turn.turn_id}.jsonl")
        self._ui.report(envelope, ledger_path)
        return TurnOutcome(answer=envelope, ledger_path=ledger_path, state=TurnState.FAILED)
