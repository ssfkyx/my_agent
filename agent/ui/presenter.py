"""presenter — 把 Envelope 投影成一行可显示的文本。

Module: agent.ui.presenter
Responsibility: 决定"这条事件显示成什么"（文本 + 颜色 token），不含 IO。
Depends on: stdlib (dataclasses, json) + agent.core.protocol
Depended by: ui.console, (后续) ui.web
ADRs: ADR-001（只消费信封，不读内部状态）, ADR-006（TB-0 行式监督）
"""

import json
from dataclasses import dataclass
from typing import Any, Mapping

from agent.core.protocol import Envelope, MessageKind

_KIND_TOKEN: Mapping[MessageKind, str] = {
    MessageKind.USER_INPUT: "accent",
    MessageKind.PLAN: "plan",
    MessageKind.RETRY: "warn",
    MessageKind.TOOL_CALL: "supervise",
    MessageKind.TOOL_RESULT: "supervise",
    MessageKind.CONSENT_REQUEST: "warn",
    MessageKind.ROLE_SWITCH: "accent",
    MessageKind.REVIEW: "review",
    MessageKind.REFLECTION: "review",
    MessageKind.ANSWER: "ok",
    MessageKind.RUN_REPORT: "muted",
    MessageKind.ERROR: "err",
}


@dataclass(frozen=True, slots=True)
class RenderedLine:
    """一行待显示的文本及其颜色 token。

    前置条件：text 不含换行。
    后置条件：token 必在 Theme.palette 的键集合内。
    禁止事项：不要在 token 中表达布局（布局属于 console）。
    """

    token: str
    text: str


class Presenter:
    """信封到显示行的纯函数式投影器。

    前置条件：无。
    后置条件：对同一信封恒产生同一行（不依赖时间或随机数）。
    禁止事项：禁止格式化整段多行内容（多行属于 report）。
    """

    def project(self, envelope: Envelope) -> RenderedLine:
        """投影一条事件。

        前置条件：envelope 已通过协议校验。
        后置条件：返回 RenderedLine；未知 kind 回落到 ERROR 样式并注明种类。
        禁止事项：不要修改 envelope 或读 ledger 之外的全局状态。
        异常：无。
        """
        seq = envelope.meta.get("seq", 0)
        detail = self._detail(envelope)
        text = f"{seq:02d} {str(envelope.kind):<11} {detail}"
        return RenderedLine(token=_KIND_TOKEN.get(envelope.kind, "err"), text=text)

    def _detail(self, envelope: Envelope) -> str:
        """按 kind 抽取一行摘要。

        前置条件：payload 为协议允许的形状。
        后置条件：返回单行、已折叠空白、至多 100 字符的文本。
        禁止事项：不要在此做嵌套 JSON 美化（会破坏单行布局）。
        异常：无。
        """
        payload = envelope.payload
        if envelope.kind is MessageKind.USER_INPUT:
            return _short(payload.get("text"))
        if envelope.kind is MessageKind.PLAN:
            steps = payload.get("steps") or []
            goal = _short(payload.get("goal"))
            return f"{goal} | {len(steps)} step(s)"
        if envelope.kind is MessageKind.RETRY:
            stage = payload.get("stage")
            attempt = payload.get("attempt")
            limit = payload.get("limit")
            return f"{stage} attempt {attempt}/{limit}: {_short(payload.get('reason'))}"
        if envelope.kind is MessageKind.TOOL_CALL:
            return f"{payload.get('tool')} {_short(payload.get('args'))}"
        if envelope.kind is MessageKind.TOOL_RESULT:
            if payload.get("ok"):
                return f"ok {_short(payload.get('data'))}"
            error = payload.get("error") or {}
            return f"failed {error.get('code')}: {_short(error.get('message'))}"
        if envelope.kind is MessageKind.CONSENT_REQUEST:
            decision = payload.get("decision", "?")
            return (
                f"{decision} tool={payload.get('tool')} "
                f"attempts={payload.get('attempts')}/{payload.get('limit')} "
                f"preview={_short(payload.get('preview'))}"
            )
        if envelope.kind is MessageKind.ROLE_SWITCH:
            return (
                f"{payload.get('from')} -> {payload.get('to')} "
                f"alias={payload.get('alias')} changed={payload.get('changed')}"
            )
        if envelope.kind is MessageKind.REVIEW:
            issues = payload.get("issues") or []
            score = float(payload.get("score", 0.0))
            return f"{payload.get('verdict')} score={score:.2f} issues={len(issues)}"
        if envelope.kind is MessageKind.REFLECTION:
            lessons = payload.get("lessons") or []
            return f"{len(lessons)} lesson(s): {_short(lessons[0]) if lessons else 'none'}"
        if envelope.kind is MessageKind.ANSWER:
            return _short(payload.get("text"))
        if envelope.kind is MessageKind.RUN_REPORT:
            verdict = (payload.get("review") or {}).get("verdict", "n/a")
            return (
                f"{float(payload.get('duration_s', 0.0)):.2f}s retries={payload.get('retries')} "
                f"failures={len(payload.get('failures') or [])} review={verdict}"
            )
        if envelope.kind is MessageKind.ERROR:
            return f"{payload.get('code')}: {_short(payload.get('message'))}"
        return "unrecognised event"


def _short(value: Any, limit: int = 100) -> str:
    """把任意值压成单行短文本。

    前置条件：value 可为 None/dict/list/标量。
    后置条件：返回不含换行、长度 <= limit 的字符串。
    禁止事项：不要对 dict 做递归美化（只做紧凑序列化）。
    异常：无（不可序列化时退化为 repr）。
    """
    if value is None:
        return "-"
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            text = repr(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
