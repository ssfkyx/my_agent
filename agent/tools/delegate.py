"""delegate — 角色委派工具：切换身份、要草稿、要第二意见。

Module: agent.tools.delegate
Responsibility: 暴露 `delegate.switch_role` / `delegate.draft` / `delegate.review`。
    后两者使用 review 模型（sub）完成一次生成，并在成功后把当前角色切到 review；
    不新开 Turn、不清上下文。
Depends on: stdlib (time) + agent.core.{learning,protocol} + agent.ports
Depended by: agent.app
ADRs: ADR-014（role 路由）, ADR-023（角色切换与 ROLE_SWITCH）, ADR-024（草稿与第二意见）
"""

import time

from agent.core.learning import REVIEW_SCHEMA, Review
from agent.core.protocol import ErrorCode, ErrorInfo, ProtocolError, ToolCall, ToolResult, ToolSpec, require_text
from agent.ports import BrainError, BrainPort, BrainRequest, RoleError, RolePort

_SWITCH_SCHEMA = {
    "type": "object",
    "properties": {
        "role": {
            "type": "string",
            "enum": ["main", "review"],
            "description": "target role for subsequent main-brain calls",
        }
    },
    "required": ["role"],
}

_DRAFT_SCHEMA = {
    "type": "object",
    "properties": {"draft": {"type": "string"}},
    "required": ["draft"],
}

_DELEGATE_MAX_TOKENS = 1024

_DRAFT_PROMPT = """You are the drafter role of a local agent. Write a concise, useful draft
answer to the request below, using only the provided context. Reply with JSON only:
{"draft": string}. Do not claim actions that were not performed. If the context is insufficient,
say what is missing in the draft itself."""

_REVIEW_PROMPT = """You are the independent reviewer role of a local agent. Judge the DRAFT
against the QUESTION and EVIDENCE below. Reply with JSON only.

Rules:
- verdict=PASS only if the draft is supported by the evidence and addresses the question.
- verdict=REVISE if the draft is fixable (missing evidence, unsupported claim, wrong focus).
- verdict=FAIL if the draft contradicts the evidence or does not attempt the request.
- score is 0..1 (0 = useless, 1 = fully supported).
- issues: at most 3 concrete defects, each under 240 characters.
- suggestions: at most 3, each under 240 characters.
- Never invent facts. Be terse: long prose truncates your JSON output.

Question:
{question}

Evidence:
{evidence}"""


class SwitchRoleTool:
    """切换当前主脑角色的工具。

    前置条件：roles 已登记 main/review；同一进程内顺序调用。
    后置条件：成功时 registry.current 变为目标角色；ToolResult.data 含 from/to/alias/changed。
    禁止事项：不要在这里加载模型、清空 ledger 或开新 Turn（加载由下一次调用触发）。
    """

    def __init__(self, *, roles: RolePort) -> None:
        self._roles = roles

    @property
    def spec(self) -> ToolSpec:
        """返回工具自描述。

        前置条件：构造时 roles 已注入。
        后置条件：同一实例多次返回等价 ToolSpec。
        禁止事项：不要在 description 里写实现细节。
        异常：无。
        """
        return ToolSpec(
            name="delegate.switch_role",
            category="delegate",
            description=(
                "Switch the active model role (main/review) for subsequent main-brain calls "
                "in this turn. Does not start a new turn or clear context."
            ),
            args_schema=_SWITCH_SCHEMA,
            side_effect=True,
        )

    def invoke(self, call: ToolCall) -> ToolResult:
        """执行角色切换。

        前置条件：call.args["role"] 为非空字符串。
        后置条件：成功返回 from/to/alias/changed；未知角色返回 BAD_ARGS，不回落到 main。
        禁止事项：不要捕获所有异常后仍报成功（会让 ROLE_SWITCH 事件失真）。
        异常：无（RoleError 被转成 ToolResult）。
        """
        started = time.perf_counter()
        raw_role = call.args.get("role")
        if not isinstance(raw_role, str) or not raw_role.strip():
            return _fail(
                call_id=call.call_id,
                message="args.role must be a non-empty string",
                started=started,
                hint="use role=main or role=review",
            )
        role = raw_role.strip()
        before = self._roles.current
        try:
            alias = self._roles.switch_role(role)
        except RoleError as exc:
            return _fail(call_id=call.call_id, message=str(exc), started=started, hint="use role=main or role=review")
        return ToolResult(
            call_id=call.call_id,
            ok=True,
            data={
                "from": before,
                "to": role,
                "alias": alias,
                "changed": before != role,
            },
            elapsed_s=time.perf_counter() - started,
        )


class DelegateDraftTool:
    """让 drafter（sub）生成一份草稿，并在成功后切到 review 角色。

    前置条件：brain 支持 role="review"；roles 已登记 review。
    后置条件：成功返回 draft/from/to/alias/changed；失败不改变当前角色。
    禁止事项：不要在生成失败时切换角色；不要把草稿直接当成最终答案（仍需 Harness 作答）。
    """

    def __init__(self, *, brain: BrainPort, roles: RolePort) -> None:
        self._brain = brain
        self._roles = roles

    @property
    def spec(self) -> ToolSpec:
        """返回工具自描述。

        前置条件：构造时 brain/roles 已注入。
        后置条件：同一实例多次返回等价 ToolSpec。
        禁止事项：不要在 description 里承诺自动交付（工具只返回草稿）。
        异常：无。
        """
        return ToolSpec(
            name="delegate.draft",
            category="delegate",
            description=(
                "Ask the drafter model for a draft answer to a focused prompt. "
                "Switches the active role to review for subsequent calls; does not clear context."
            ),
            args_schema={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "What the draft should address."},
                    "context": {"type": "string", "description": "Optional evidence or constraints."},
                },
                "required": ["prompt"],
            },
            side_effect=True,
        )

    def invoke(self, call: ToolCall) -> ToolResult:
        """生成草稿。

        前置条件：args.prompt 为非空字符串；args.context 可为空。
        后置条件：成功返回 draft 与角色切换信息；模型失败返回 UPSTREAM_ERROR。
        禁止事项：不要在失败时把 registry.current 改成 review。
        异常：无。
        """
        started = time.perf_counter()
        prompt = call.args.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return _fail(
                call_id=call.call_id,
                message="args.prompt must be a non-empty string",
                started=started,
                hint='call with {"prompt": "<what to draft>"}',
            )
        context = _optional_text(call.args.get("context")) or "(none)"
        before = self._roles.current
        try:
            self._roles.resolve("review")
            reply = self._brain.complete(
                BrainRequest(
                    messages=(
                        {"role": "system", "content": _DRAFT_PROMPT},
                        {"role": "user", "content": f"Request:\n{prompt.strip()}\n\nContext:\n{context}"},
                    ),
                    json_schema=_DRAFT_SCHEMA,
                    temperature=0.2,
                    max_tokens=_DELEGATE_MAX_TOKENS,
                    role="review",
                    disable_thinking=True,
                )
            )
            draft = require_text(reply.content.get("draft"), "draft")
        except RoleError as exc:
            return _fail(
                call_id=call.call_id,
                message=str(exc),
                started=started,
                code=ErrorCode.UPSTREAM_ERROR,
                hint="configure a review role/alias",
            )
        except (BrainError, ProtocolError) as exc:
            return _fail(
                call_id=call.call_id,
                message=f"draft model failed: {exc}",
                started=started,
                code=ErrorCode.UPSTREAM_ERROR,
                retryable=True,
            )
        return self._finish(call.call_id, before, {"draft": draft.strip()}, started)

    def _finish(
        self,
        call_id: str,
        before: str,
        payload: dict[str, object],
        started: float,
    ) -> ToolResult:
        """模型成功后切换角色并组装回执。

        前置条件：模型调用已成功；review 角色已由调用方 resolve 过。
        后置条件：registry.current == "review"；data 含 from/to/alias/changed 与 payload。
        禁止事项：不要在切换失败时报告成功。
        异常：无。
        """
        try:
            alias = self._roles.switch_role("review")
        except RoleError as exc:
            return _fail(
                call_id=call_id,
                message=f"draft generated but role switch failed: {exc}",
                started=started,
                code=ErrorCode.INTERNAL,
            )
        return ToolResult(
            call_id=call_id,
            ok=True,
            data={
                "from": before,
                "to": "review",
                "alias": alias,
                "changed": before != "review",
                **payload,
            },
            elapsed_s=time.perf_counter() - started,
        )


class DelegateReviewTool:
    """让 reviewer（sub）对一份草稿给出第二意见，并在成功后切到 review 角色。

    前置条件：brain 支持 role="review"；roles 已登记 review。
    后置条件：成功返回 review 结构（verdict/score/issues/suggestions）与角色切换信息。
    禁止事项：不要在评审失败时切换角色；不要改写草稿（评审只读）。
    """

    def __init__(self, *, brain: BrainPort, roles: RolePort) -> None:
        self._brain = brain
        self._roles = roles

    @property
    def spec(self) -> ToolSpec:
        """返回工具自描述。

        前置条件：构造时 brain/roles 已注入。
        后置条件：同一实例多次返回等价 ToolSpec。
        禁止事项：不要在 description 里承诺自动修复草稿。
        异常：无。
        """
        return ToolSpec(
            name="delegate.review",
            category="delegate",
            description=(
                "Get an independent review of a draft answer, with verdict/score/issues/suggestions. "
                "Switches the active role to review for subsequent calls; does not clear context."
            ),
            args_schema={
                "type": "object",
                "properties": {
                    "draft": {"type": "string", "description": "Draft answer to review."},
                    "question": {"type": "string", "description": "Original user question (optional)."},
                    "evidence": {"type": "string", "description": "Evidence the draft should rely on (optional)."},
                },
                "required": ["draft"],
            },
            side_effect=True,
        )

    def invoke(self, call: ToolCall) -> ToolResult:
        """评审草稿。

        前置条件：args.draft 为非空字符串；question/evidence 可为空。
        后置条件：成功返回 review 与角色切换信息；模型失败返回 UPSTREAM_ERROR。
        禁止事项：不要在失败时把 registry.current 改成 review。
        异常：无。
        """
        started = time.perf_counter()
        draft = call.args.get("draft")
        if not isinstance(draft, str) or not draft.strip():
            return _fail(
                call_id=call.call_id,
                message="args.draft must be a non-empty string",
                started=started,
                hint='call with {"draft": "<text to review>"}',
            )
        question = _optional_text(call.args.get("question")) or "(not provided)"
        evidence = _optional_text(call.args.get("evidence")) or "(none)"
        before = self._roles.current
        try:
            self._roles.resolve("review")
            reply = self._brain.complete(
                BrainRequest(
                    messages=(
                        {
                            "role": "system",
                            "content": _REVIEW_PROMPT.format(question=question, evidence=evidence),
                        },
                        {"role": "user", "content": draft.strip()},
                    ),
                    json_schema=REVIEW_SCHEMA,
                    temperature=0.2,
                    max_tokens=_DELEGATE_MAX_TOKENS,
                    role="review",
                    disable_thinking=True,
                )
            )
            review = Review.from_payload(reply.content)
        except RoleError as exc:
            return _fail(
                call_id=call.call_id,
                message=str(exc),
                started=started,
                code=ErrorCode.UPSTREAM_ERROR,
                hint="configure a review role/alias",
            )
        except (BrainError, ProtocolError) as exc:
            return _fail(
                call_id=call.call_id,
                message=f"review model failed: {exc}",
                started=started,
                code=ErrorCode.UPSTREAM_ERROR,
                retryable=True,
            )
        return self._finish(call.call_id, before, {"review": review.to_payload()}, started)

    def _finish(
        self,
        call_id: str,
        before: str,
        payload: dict[str, object],
        started: float,
    ) -> ToolResult:
        """模型成功后切换角色并组装回执。

        前置条件：模型调用已成功；review 角色已由调用方 resolve 过。
        后置条件：registry.current == "review"；data 含 from/to/alias/changed 与 payload。
        禁止事项：不要在切换失败时报告成功。
        异常：无。
        """
        try:
            alias = self._roles.switch_role("review")
        except RoleError as exc:
            return _fail(
                call_id=call_id,
                message=f"review generated but role switch failed: {exc}",
                started=started,
                code=ErrorCode.INTERNAL,
            )
        return ToolResult(
            call_id=call_id,
            ok=True,
            data={
                "from": before,
                "to": "review",
                "alias": alias,
                "changed": before != "review",
                **payload,
            },
            elapsed_s=time.perf_counter() - started,
        )


def _optional_text(value: object) -> str:
    """把可选字符串参数归一化成去空白文本。

    前置条件：value 可为 None/str/其他类型。
    后置条件：str 返回 strip 结果；其他类型返回空串。
    禁止事项：不要对非字符串做 str() 强转（会把结构误当提示词）。
    异常：无。
    """
    return value.strip() if isinstance(value, str) else ""


def _fail(
    *,
    call_id: str,
    message: str,
    started: float,
    code: ErrorCode = ErrorCode.BAD_ARGS,
    retryable: bool = False,
    hint: str = "",
) -> ToolResult:
    """构造一个失败回执。

    前置条件：message 非空；code 为协议错误码。
    后置条件：返回 ok=False 的 ToolResult；retryable 由调用方明确指定。
    禁止事项：不要为非法参数或失败模型调用返回成功。
    异常：无。
    """
    return ToolResult(
        call_id=call_id,
        ok=False,
        error=ErrorInfo(code=code, message=message, retryable=retryable, hint=hint),
        elapsed_s=time.perf_counter() - started,
    )
