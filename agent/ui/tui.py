"""tui — 单窗口 curses 界面：计划 / 监督 / 对话 / 底部状态四区。

Module: agent.ui.tui
Responsibility: 实现 UiPort + InteractiveUiPort：消费 Envelope 渲染四区布局，
    在空闲时读取多轮用户输入，在升级前就地询问同意。
Depends on: stdlib (curses, textwrap) + agent.core.protocol + agent.ui.presenter
Depended by: agent.app（--tui 模式）
ADRs: ADR-003（stdlib curses）, ADR-006（UI 只消费信封）, ADR-021（同意询问）,
    pre_architecture §9（四区布局与配色 token）
"""

import curses
import textwrap
from pathlib import Path
from typing import Any

from agent.core.protocol import Envelope, MessageKind
from agent.ui.presenter import Presenter

# token 名与 Theme/§9 配色表一致；curses 用基础 8 色近似。
_TOKEN_COLOR: dict[str, int] = {
    "plan": curses.COLOR_YELLOW,
    "supervise": curses.COLOR_CYAN,
    "ok": curses.COLOR_GREEN,
    "warn": curses.COLOR_YELLOW,
    "err": curses.COLOR_RED,
    "review": curses.COLOR_GREEN,
    "memory": curses.COLOR_MAGENTA,
    "muted": curses.COLOR_WHITE,
    "accent": curses.COLOR_MAGENTA,
}
_PAIR_INDEX: dict[str, int] = {token: index for index, token in enumerate(_TOKEN_COLOR, start=1)}


class TuiUI:
    """curses 单窗口 UI（实现 UiPort + InteractiveUiPort）。

    前置条件：在 TTY 主线程中构造；consent_mode ∈ {ask, allow, deny}。
    后置条件：render/report 只重绘本对象持有的快照；read_user_input 阻塞读一行。
    禁止事项：不要在这里调用 Harness/core；不要跨线程刷新同一个 curses screen。
    """

    def __init__(self, *, consent_mode: str = "ask") -> None:
        if consent_mode not in {"ask", "allow", "deny"}:
            raise ValueError("consent_mode must be one of: ask, allow, deny")
        self._consent_mode = consent_mode
        self._presenter = Presenter()
        self._closed = False
        self._events: list[tuple[str, str]] = []
        self._plan_steps: list[dict[str, Any]] = []
        self._step_status: dict[str, str] = {}
        self._conversation: list[tuple[str, str]] = []
        self._review_line = "review: -"
        self._report_line = "report: -"
        self._ledger = "ledger: -"
        self._turn_id = ""
        self._role = "main"
        self._status = "idle"
        self._consent_question = ""
        self._input = ""
        self._scroll = 0
        self._visible_supervision_rows = 1

        try:
            self._stdscr = curses.initscr()
        except curses.error as exc:
            raise RuntimeError(f"cannot initialize curses UI: {exc}") from exc
        try:
            curses.noecho()
            curses.cbreak()
            self._stdscr.keypad(True)
            self._init_colors()
            try:
                curses.curs_set(1)
            except curses.error:
                pass
        except Exception:
            # 初始化中途失败时尽量恢复终端，避免把 shell 留在 raw mode。
            try:
                curses.nocbreak()
                curses.echo()
                curses.endwin()
            except curses.error:
                pass
            raise
        self._redraw()

    # ---- UiPort ---------------------------------------------------------

    def render(self, envelope: Envelope) -> None:
        """接收一条事件、更新快照并重绘。

        前置条件：envelope 已通过协议校验。
        后置条件：事件进入滚动监督区；计划/对话/角色状态按事件更新。
        禁止事项：不要把渲染错误抛回 Harness。
        异常：无。
        """
        if self._closed:
            return
        try:
            self._ingest(envelope)
            self._redraw()
        except curses.error:
            # 终端尺寸变化等瞬时错误：下一次事件会重绘。
            pass

    def report(self, envelope: Envelope, ledger_path: Path) -> None:
        """记录最终结论与 ledger 路径。

        前置条件：envelope.kind 为 ANSWER 或 ERROR。
        后置条件：底部显示 ledger；状态回到 idle。
        禁止事项：不要在这里重复追加 ANSWER 到监督区（render 已追加）。
        异常：无。
        """
        self._ledger = f"ledger: {ledger_path}"
        self._status = "idle" if envelope.kind is MessageKind.ANSWER else "failed"
        self._redraw()

    def ask_consent(self, question: str) -> bool:
        """在底部就地询问是否允许外发升级。

        前置条件：question 已由 Harness 脱敏。
        后置条件：allow/deny 模式直接返回；ask 模式读入 y/yes 才返回 True。
        禁止事项：不要在非 TTY 环境自动同意（TUI 只在 TTY 启动）。
        异常：无。
        """
        if self._consent_mode == "allow":
            return True
        if self._consent_mode == "deny":
            return False
        self._consent_question = question
        self._redraw()
        result = False
        while not self._closed:
            try:
                key = self._stdscr.get_wch()
            except curses.error:
                continue
            except KeyboardInterrupt:
                break
            if isinstance(key, str):
                lowered = key.casefold()
                if lowered == "y":
                    result = True
                    break
                if key in {"n", "\n", "\r", "\x1b"}:
                    break
            elif key == curses.KEY_RESIZE:
                self._redraw()
        self._consent_question = ""
        self._redraw()
        return result

    # ---- InteractiveUiPort ---------------------------------------------

    def read_user_input(self) -> str | None:
        """读取一行用户输入。

        前置条件：当前不在 turn 中。
        后置条件：Enter 返回文本；Esc/Ctrl-C/Ctrl-D 返回 None；/quit 交给调用方处理。
        禁止事项：不要在此调用模型或工具。
        异常：无（KeyboardInterrupt 按 None 处理）。
        """
        self._status = "idle"
        self._input = ""
        self._redraw()
        while not self._closed:
            try:
                key = self._stdscr.get_wch()
            except KeyboardInterrupt:
                return None
            except curses.error:
                continue
            if isinstance(key, str):
                if key in {"\n", "\r"}:
                    text = self._input
                    self._input = ""
                    return text
                if key in {"\x03", "\x04", "\x1b"}:
                    return None
                if key in {"\x7f", "\b"}:
                    self._input = self._input[:-1]
                elif key.isprintable() and len(self._input) < 400:
                    self._input += key
            elif key in {curses.KEY_BACKSPACE, 8, 127}:
                self._input = self._input[:-1]
            elif key in {curses.KEY_ENTER, 10, 13}:
                text = self._input
                self._input = ""
                return text
            elif key == curses.KEY_RESIZE:
                pass
            elif key == curses.KEY_PPAGE:
                self._scroll += self._visible_supervision_rows
            elif key == curses.KEY_NPAGE:
                self._scroll = max(0, self._scroll - self._visible_supervision_rows)
            elif key == curses.KEY_HOME:
                self._scroll = 10**9
            elif key == curses.KEY_END:
                self._scroll = 0
            self._redraw()
        return None

    def show_error(self, message: str) -> None:
        """追加一条非致命错误并保持界面可用。

        前置条件：message 非空。
        后置条件：错误进入监督区；状态为 error。
        禁止事项：不要退出 curses。
        异常：无。
        """
        if self._closed:
            return
        self._events.append(("err", f"runtime: {message}"))
        self._events = self._events[-120:]
        self._status = "error"
        self._redraw()

    def close(self) -> None:
        """恢复终端状态。

        前置条件：无。
        后置条件：可重复调用；之后 render 不再画屏。
        禁止事项：不要在 close 后继续 get_wch。
        异常：无。
        """
        if self._closed:
            return
        self._closed = True
        try:
            self._stdscr.keypad(False)
            curses.nocbreak()
            curses.echo()
            curses.endwin()
        except curses.error:
            pass

    # ---- state ingestion ------------------------------------------------

    def _ingest(self, envelope: Envelope) -> None:
        """把一条事件并入界面快照。"""
        payload = envelope.payload
        kind = envelope.kind
        if kind is MessageKind.USER_INPUT:
            self._turn_id = envelope.turn_id
            self._conversation.append(("user", str(payload.get("text", ""))))
            self._status = "planning"
        elif kind is MessageKind.PLAN:
            self._plan_steps = []
            steps = payload.get("steps") or []
            for index, step in enumerate(steps, start=1):
                if not isinstance(step, dict):
                    continue
                self._plan_steps.append(
                    {
                        "step_id": f"s-{index}",
                        "kind": step.get("kind", "?"),
                        "target": step.get("target", ""),
                        "why": step.get("why", ""),
                    }
                )
            self._step_status = {step["step_id"]: "pending" for step in self._plan_steps}
            self._status = "acting"
        elif kind is MessageKind.TOOL_CALL:
            step_id = envelope.meta.get("step_id")
            if isinstance(step_id, str):
                self._step_status[step_id] = "running"
            self._status = "acting"
        elif kind is MessageKind.TOOL_RESULT:
            step_id = envelope.meta.get("step_id")
            if isinstance(step_id, str):
                self._step_status[step_id] = "ok" if payload.get("ok") else "fail"
        elif kind is MessageKind.RETRY:
            self._status = "retrying"
        elif kind is MessageKind.CONSENT_REQUEST:
            self._status = "awaiting consent"
        elif kind is MessageKind.ROLE_SWITCH:
            self._role = str(payload.get("to") or self._role)
        elif kind is MessageKind.REVIEW:
            issues = payload.get("issues") or []
            self._review_line = (
                f"review: {payload.get('verdict')} "
                f"score={float(payload.get('score', 0.0)):.2f} issues={len(issues)}"
            )
            self._status = "reviewing"
        elif kind is MessageKind.ANSWER:
            self._conversation.append(("agent", str(payload.get("text", ""))))
            self._status = "idle"
        elif kind is MessageKind.ERROR:
            self._conversation.append(
                ("error", f"{payload.get('code')}: {payload.get('message', '')}")
            )
            self._status = "failed"
        elif kind is MessageKind.RUN_REPORT:
            self._report_line = (
                f"report: {float(payload.get('duration_s', 0.0)):.1f}s "
                f"retries={payload.get('retries')} failures={len(payload.get('failures') or [])}"
            )
            self._status = "idle"

        line = self._presenter.project(envelope)
        self._events.append((line.token, line.text))
        self._events = self._events[-120:]

    # ---- drawing --------------------------------------------------------

    def _init_colors(self) -> None:
        """初始化颜色对；终端不支持时静默退化。"""
        if not curses.has_colors():
            return
        curses.start_color()
        try:
            curses.use_default_colors()
            background = -1
        except curses.error:
            background = curses.COLOR_BLACK
        for token, color in _TOKEN_COLOR.items():
            try:
                curses.init_pair(_PAIR_INDEX[token], color, background)
            except curses.error:
                pass

    def _pair(self, token: str) -> int:
        """返回 token 对应的 curses 属性；无色终端返回 0。"""
        if not curses.has_colors():
            return 0
        index = _PAIR_INDEX.get(token)
        return curses.color_pair(index) if index is not None else 0

    def _redraw(self) -> None:
        """按当前快照重绘整个屏幕。"""
        if self._closed:
            return
        try:
            self._stdscr.erase()
            height, width = self._stdscr.getmaxyx()
        except curses.error:
            return
        if height < 10 or width < 50:
            self._draw_minimal(height, width)
        else:
            self._draw_full(height, width)
        try:
            self._stdscr.refresh()
        except curses.error:
            pass

    def _draw_full(self, height: int, width: int) -> None:
        """四区布局：计划/监督 + 对话 + 底部状态 + 输入行。"""
        header = f" AGENT  turn={self._turn_id or '-'}  role={self._role}  status={self._status}"
        self._add(0, 0, header[: width - 1], self._pair("accent"))

        body_top = 1
        body_height = max(3, height - 10)
        plan_width = max(28, int(width * 0.42))
        supervision_x = plan_width + 1
        supervision_width = max(10, width - supervision_x)

        plan_lines = self._plan_lines(plan_width - 4)
        self._panel(body_top, 0, body_height, plan_width, "PLAN", plan_lines)

        all_supervision = self._wrapped_events(supervision_width - 4)
        visible_rows = max(1, body_height - 2)
        self._visible_supervision_rows = visible_rows
        max_scroll = max(0, len(all_supervision) - visible_rows)
        self._scroll = min(self._scroll, max_scroll)
        end = len(all_supervision) - self._scroll
        start = max(0, end - visible_rows)
        supervision_lines = all_supervision[start:end]
        title = "SUPERVISION" + (f" +{self._scroll}" if self._scroll else "")
        self._panel(body_top, supervision_x, body_height, supervision_width, title, supervision_lines)

        conversation_top = body_top + body_height
        conversation_height = 4
        conversation_lines = self._conversation_lines(width - 4)[-(conversation_height - 2) :]
        self._panel(conversation_top, 0, conversation_height, width, "CONVERSATION / ANSWER", conversation_lines)

        if self._consent_question:
            # 同意问题可能很长：临时占用对话/底部区域，保证预览和提示都可见。
            consent_top = conversation_top
            consent_height = max(3, height - 1 - consent_top)
            consent_lines = [("warn", part) for part in _wrap(self._consent_question, width - 4)]
            consent_lines.append(("warn", "Allow external escalation? [y/N]"))
            self._panel(consent_top, 0, consent_height, width, "CONSENT", consent_lines)
            self._draw_input_line(height, width)
            return

        footer_top = conversation_top + conversation_height
        footer_height = max(1, height - 1 - footer_top)
        footer_lines = self._footer_lines()
        for index in range(footer_height):
            text = footer_lines[index] if index < len(footer_lines) else ""
            self._add(footer_top + index, 1, text[: width - 2], self._pair("muted"))

        self._draw_input_line(height, width)

    def _draw_minimal(self, height: int, width: int) -> None:
        """小终端降级：只显示最近事件与输入行。"""
        suffix = f" +{self._scroll}" if self._scroll else ""
        self._add(
            0,
            0,
            f"AGENT {self._turn_id} [{self._role}] {self._status}{suffix}"[: width - 1],
            self._pair("accent"),
        )
        all_lines = self._wrapped_events(max(1, width - 2))
        visible_rows = max(1, height - 3)
        self._visible_supervision_rows = visible_rows
        max_scroll = max(0, len(all_lines) - visible_rows)
        self._scroll = min(self._scroll, max_scroll)
        end = len(all_lines) - self._scroll
        start = max(0, end - visible_rows)
        lines = all_lines[start:end]
        for index, (token, text) in enumerate(lines, start=1):
            self._add(index, 0, text[: width - 1], self._pair(token))
        self._draw_input_line(height, width)

    def _draw_input_line(self, height: int, width: int) -> None:
        """画底部输入行与光标。"""
        if self._consent_question:
            prompt = "consent> [y/N] "
            attr = self._pair("warn")
        else:
            prompt = "user> "
            attr = self._pair("accent")
        text = prompt + self._input
        visible = text if len(text) < width else text[-(width - 1) :]
        self._add(height - 1, 0, visible, attr)
        try:
            self._stdscr.move(height - 1, min(width - 1, len(visible)))
        except curses.error:
            pass

    def _panel(
        self,
        top: int,
        left: int,
        height: int,
        width: int,
        title: str,
        lines: list[tuple[str, str]],
    ) -> None:
        """画一个带边框的面板；超长内容裁切。"""
        if height < 3 or width < 10:
            return
        border = "+" + "-" * (width - 2) + "+"
        self._add(top, left, border, self._pair("muted"))
        self._add(top, left + 2, f" {title} "[: width - 4], self._pair("accent"))
        for row in range(1, height - 1):
            self._add(top + row, left, "|", self._pair("muted"))
            self._add(top + row, left + width - 1, "|", self._pair("muted"))
            if row - 1 < len(lines):
                token, text = lines[row - 1]
                self._add(top + row, left + 2, text[: width - 4], self._pair(token))
        self._add(top + height - 1, left, border, self._pair("muted"))

    def _plan_lines(self, width: int) -> list[tuple[str, str]]:
        """把计划步骤投影成带状态标记的行。"""
        if not self._plan_steps:
            return [("muted", "(waiting for plan)")]
        marks = {"pending": ".", "running": ">", "ok": "+", "fail": "x"}
        lines: list[tuple[str, str]] = []
        for step in self._plan_steps:
            step_id = step["step_id"]
            mark = marks.get(self._step_status.get(step_id, "pending"), ".")
            target = step["target"] or "answer"
            text = f"{mark} {step_id} {step['kind']}:{target}  {step['why']}"
            for part in _wrap(text, width):
                lines.append(("plan", part))
        return lines

    def _wrapped_events(self, width: int) -> list[tuple[str, str]]:
        """把监督事件展开成按宽度换行的行。"""
        lines: list[tuple[str, str]] = []
        for token, text in self._events:
            for part in _wrap(text, width):
                lines.append((token, part))
        return lines

    def _conversation_lines(self, width: int) -> list[tuple[str, str]]:
        """投影最近几条用户/助手消息。"""
        if not self._conversation:
            return [("muted", "(no messages yet)")]
        lines: list[tuple[str, str]] = []
        for speaker, text in self._conversation[-3:]:
            prefix = {"user": "user> ", "agent": "agent> ", "error": "error> "}.get(speaker, "> ")
            token = {"user": "accent", "agent": "ok", "error": "err"}.get(speaker, "muted")
            for part in _wrap(prefix + text, width):
                lines.append((token, part))
        return lines

    def _footer_lines(self) -> list[str]:
        """底部状态：review/report/ledger 与按键提示。"""
        return [
            self._review_line,
            self._report_line,
            self._ledger,
            "Enter send | PgUp/PgDn scroll | /quit exit | Ctrl-C cancel/quit",
        ]

    def _add(self, y: int, x: int, text: str, attr: int = 0) -> None:
        """安全写屏：越界或 curses 错误一律吞掉。"""
        try:
            height, width = self._stdscr.getmaxyx()
            if y < 0 or y >= height or x < 0 or x >= width:
                return
            self._stdscr.addnstr(y, x, text, max(0, width - x), attr)
        except curses.error:
            pass


def _wrap(text: str, width: int) -> list[str]:
    """按显示宽度换行，保留空行。

    前置条件：width >= 1。
    后置条件：返回至少一行；不抛异常。
    禁止事项：不要依赖终端真实显示宽度（中文按 1 列近似）。
    异常：无。
    """
    width = max(1, width)
    lines: list[str] = []
    for raw in str(text).splitlines() or [""]:
        lines.extend(textwrap.wrap(raw, width=width) or [""])
    return lines
