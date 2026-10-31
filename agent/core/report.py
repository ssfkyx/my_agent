"""report — 一轮运行的监督报告（RunReport）与渲染。

Module: agent.core.report
Responsibility: 把 ledger 事实 + 计划 + 评审汇成 RunReport，并渲染成 JSON/Markdown 两份。
Depends on: stdlib (dataclasses, pathlib, typing) + agent.core.{protocol,plan,learning}
Depended by: core.harness, app
ADRs: ADR-013（报告契约与事件顺序）, ADR-003（耗时一律 FP64）,
    ADR-034（实际 ngl 写入报告）
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from agent.core.learning import Review
from agent.core.plan import Plan
from agent.core.protocol import Envelope, MessageKind
from agent.core.turn import Turn


@dataclass(frozen=True, slots=True)
class RunReport:
    """一轮运行的事后总结（机读 + 人读同源）。

    前置条件：由 Reporter.build 构造；duration_s 为 FP64 秒。
    后置条件：to_payload 与 to_markdown 描述同一份事实，不得各自增删语义。
    禁止事项：不要在报告里写入 ledger 中不存在的事实（报告不是第二次创作）。
    """

    turn_id: str
    user_text: str
    goal: str
    plan_steps: tuple[Mapping[str, Any], ...]
    tool_results: tuple[Mapping[str, Any], ...]
    retries: int
    failures: tuple[str, ...]
    answer_text: str
    confidence: float
    review: Review | None
    duration_s: float
    ledger_path: Path
    timings: Mapping[str, float]
    # 本轮 ensure 过的模型及其实测 ngl（ADR-034）；None 表示使用 --fit on。
    models: Mapping[str, int | None]

    def to_payload(self) -> dict[str, Any]:
        """转为 RUN_REPORT 信封的 payload（同时用于 JSON 文件）。

        前置条件：对象由 Reporter.build 构造。
        后置条件：返回可 JSON 序列化的 dict；review 为 None 时显式输出 null。
        禁止事项：不要把 tuple 直接留在 payload 里。
        异常：无。
        """
        return {
            "turn_id": self.turn_id,
            "user_text": self.user_text,
            "goal": self.goal,
            "plan_steps": [dict(step) for step in self.plan_steps],
            "tool_results": [dict(result) for result in self.tool_results],
            "retries": self.retries,
            "failures": list(self.failures),
            "answer": {"text": self.answer_text, "confidence": self.confidence},
            "review": None if self.review is None else self.review.to_payload(),
            "duration_s": self.duration_s,
            "timings": {key: float(value) for key, value in self.timings.items()},
            "models": {alias: ngl for alias, ngl in self.models.items()},
            "ledger_path": str(self.ledger_path),
        }

    def to_markdown(self) -> str:
        """渲染成人读的 Markdown。

        前置条件：对象由 Reporter.build 构造。
        后置条件：包含计划、工具、模型、失败、答案、评审、耗时七个部分。
        禁止事项：不要隐藏失败或评审缺失（必须显式写出）。
        异常：无。
        """
        lines: list[str] = [
            f"# Run report `{self.turn_id}`",
            "",
            f"- user: {self.user_text}",
            f"- goal: {self.goal}",
            f"- duration: {self.duration_s:.3f} s",
            f"- retries: {self.retries}",
            f"- failures: {len(self.failures)}",
            f"- ledger: `{self.ledger_path}`",
            "",
            "## Plan",
            "",
            "| # | kind | target | why |",
            "| --- | --- | --- | --- |",
        ]
        for step in self.plan_steps:
            lines.append(
                f"| {step.get('step_id')} | {step.get('kind')} | {step.get('target') or '-'} | {step.get('why')} |"
            )

        lines.extend(["", "## Tool results", "", "| call | tool | ok | error | elapsed |", "| --- | --- | --- | --- | --- |"])
        for result in self.tool_results:
            error = result.get("error_code") or "-"
            elapsed = result.get("elapsed_s")
            elapsed_text = _format_seconds(elapsed)
            lines.append(
                f"| {result.get('call_id')} | {result.get('tool')} | {result.get('ok')} | {error} | {elapsed_text} |"
            )

        lines.extend(["", "## Models", ""])
        if self.models:
            lines.extend(["| alias | ngl |", "| --- | --- |"])
            for alias, ngl in self.models.items():
                lines.append(f"| {alias} | {'--fit on' if ngl is None else ngl} |")
        else:
            lines.append("_no model used this turn_")

        lines.extend(["", "## Timing", ""])
        for stage in ("plan", "review", "answer"):
            if stage in self.timings:
                lines.append(f"- {stage}: {_format_seconds(self.timings[stage])}")
        lines.append(f"- total: {_format_seconds(self.duration_s)}")

        lines.extend(["", "## Failures", ""])
        lines.extend([f"- {failure}" for failure in self.failures] or ["- none"])

        lines.extend(["", "## Answer", "", self.answer_text, "", f"_confidence={self.confidence:.2f}_"])

        lines.extend(["", "## Review", ""])
        if self.review is None:
            lines.append("_review unavailable_")
        else:
            lines.append(f"- verdict: **{self.review.verdict}**")
            lines.append(f"- score: {self.review.score:.2f}")
            lines.append("- issues: " + ("" if self.review.issues else "none"))
            lines.extend([f"  - {issue}" for issue in self.review.issues])
            lines.append("- suggestions: " + ("" if self.review.suggestions else "none"))
            lines.extend([f"  - {suggestion}" for suggestion in self.review.suggestions])
        lines.append("")
        return "\n".join(lines)


class Reporter:
    """从 ledger 事实构建 RunReport（ADR-013）。

    前置条件：无。
    后置条件：build 是纯函数（不写文件、不改 ledger）。
    禁止事项：不要在 build 中调用模型（报告只做汇总）。
    """

    def build(
        self,
        *,
        turn: Turn,
        plan: Plan,
        review: Review | None,
        answer: Envelope,
        duration_s: float,
        ledger_path: Path,
        models: Mapping[str, int | None] | None = None,
    ) -> RunReport:
        """汇总一轮的事实。

        前置条件：answer.kind 为 ANSWER；turn.ledger 已包含该轮全部事件。
        后置条件：返回 RunReport；failures 覆盖工具失败与 ERROR 事件。
        禁止事项：不要漏掉 RETRY（重试次数是验收项）。
        异常：无。
        """
        events = turn.ledger.events()
        calls = {
            payload.get("call_id"): payload.get("tool")
            for payload in (event.payload for event in events if event.kind is MessageKind.TOOL_CALL)
        }

        tool_results: list[dict[str, Any]] = []
        failures: list[str] = []
        for event in events:
            if event.kind is MessageKind.TOOL_RESULT:
                payload = event.payload
                error = payload.get("error") or {}
                call_id = payload.get("call_id")
                tool_results.append(
                    {
                        "call_id": call_id,
                        "tool": calls.get(call_id, "?"),
                        "ok": bool(payload.get("ok")),
                        "error_code": error.get("code"),
                        "error_message": error.get("message"),
                        "elapsed_s": payload.get("elapsed_s", 0.0),
                    }
                )
                if not payload.get("ok"):
                    failures.append(f"{call_id} {calls.get(call_id, '?')}: {error.get('code')} {error.get('message')}")
            elif event.kind is MessageKind.ERROR:
                failures.append(f"{event.payload.get('code')}: {event.payload.get('message')}")

        retries = sum(1 for event in events if event.kind is MessageKind.RETRY)
        timings: dict[str, float] = {}
        for event in events:
            elapsed = event.meta.get("elapsed_s")
            if not isinstance(elapsed, int | float):
                continue
            if event.kind is MessageKind.PLAN:
                timings["plan"] = float(elapsed)
            elif event.kind is MessageKind.REVIEW:
                timings["review"] = float(elapsed)
            elif event.kind is MessageKind.ANSWER:
                timings["answer"] = float(elapsed)

        plan_steps = tuple(
            {
                "step_id": step.step_id,
                "kind": str(step.kind),
                "target": step.target,
                "why": step.why,
            }
            for step in plan.steps
        )
        return RunReport(
            turn_id=turn.turn_id,
            user_text=turn.user_text,
            goal=plan.goal,
            plan_steps=plan_steps,
            tool_results=tuple(tool_results),
            retries=retries,
            failures=tuple(failures),
            answer_text=str(answer.payload.get("text", "")),
            confidence=float(answer.payload.get("confidence", 0.0)),
            review=review,
            duration_s=duration_s,
            ledger_path=ledger_path,
            timings=timings,
            models=dict(models or {}),
        )

    def write(self, report: RunReport, *, run_dir: Path) -> tuple[Path, Path]:
        """落盘 JSON 与 Markdown 两份报告。

        前置条件：run_dir 可写。
        后置条件：返回 (json_path, md_path)；两者内容同源。
        禁止事项：不要覆盖其他 turn 的报告（文件名含 turn_id）。
        异常：OSError（磁盘/权限）。
        """
        run_dir.mkdir(parents=True, exist_ok=True)
        json_path = run_dir / f"{report.turn_id}.report.json"
        md_path = run_dir / f"{report.turn_id}.report.md"
        json_path.write_text(
            json.dumps(report.to_payload(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        md_path.write_text(report.to_markdown(), encoding="utf-8")
        return json_path, md_path


def _format_seconds(value: Any) -> str:
    """把 FP64 秒渲染成人类可读的时长。

    前置条件：value 可为 None 或数值。
    后置条件：<10 ms 显示毫秒，其余显示秒（3 位小数）；None 显示 "-"。
    禁止事项：不要把毫秒四舍五入成 "0.000 s"（会掩盖真实耗时）。
    异常：无。
    """
    if not isinstance(value, (int, float)):
        return "-"
    seconds = float(value)
    if seconds < 0.01:
        return f"{seconds * 1000.0:.1f} ms"
    return f"{seconds:.3f} s"
