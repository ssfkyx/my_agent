"""console — 行式控制台 UI（TB-0；curses 交互留给 TB-2）。

Module: agent.ui.console
Responsibility: 把 Presenter 的投影写到输出流，并在 turn 结束时给出结论与审计路径。
Depends on: stdlib (sys, pathlib, typing) + agent.ui.{presenter,theme} + agent.core.protocol
Depended by: agent.app
ADRs: ADR-006（TB-0 只做单轮行式输出）, ADR-001（只消费信封）, ADR-021（同意询问）
"""

import sys
from pathlib import Path
from typing import IO

from agent.core.protocol import Envelope, MessageKind
from agent.ui.presenter import Presenter
from agent.ui.theme import Theme


class ConsoleUI:
    """行式控制台渲染器（实现 UiPort）。

    前置条件：stream 可写；presenter/theme 已构造。
    后置条件：每条事件输出一行；report 输出结论块。
    禁止事项：禁止缓存事件（UI 不是事实来源，ledger 才是）。
    """

    def __init__(
        self,
        *,
        presenter: Presenter,
        theme: Theme,
        stream: IO[str] | None = None,
        consent_mode: str = "ask",
        keyboard: IO[str] | None = None,
    ) -> None:
        if consent_mode not in {"ask", "allow", "deny"}:
            raise ValueError("consent_mode must be one of: ask, allow, deny")
        self._presenter = presenter
        self._theme = theme
        self._stream = stream if stream is not None else sys.stdout
        self._keyboard = keyboard if keyboard is not None else sys.stdin
        self._consent_mode = consent_mode

    def render(self, envelope: Envelope) -> None:
        """输出一条事件行。

        前置条件：envelope 已通过协议校验。
        后置条件：该行已 flush，便于紧随其后的模型调用观察进度。
        禁止事项：不要因输出失败而中断 harness（管道被关是常态）。
        异常：无（BrokenPipe 等输出错误被吞掉）。
        """
        line = self._presenter.project(envelope)
        self._write(self._theme.paint(line.token, line.text))

    def report(self, envelope: Envelope, ledger_path: Path) -> None:
        """输出最终结论与 ledger 位置。

        前置条件：envelope.kind 为 ANSWER 或 ERROR。
        后置条件：结论块已写出（多行），并附 ledger 路径。
        禁止事项：不要在结论里重复逐条事件。
        异常：无。
        """
        if envelope.kind is MessageKind.ANSWER:
            text = str(envelope.payload.get("text", ""))
            confidence = envelope.payload.get("confidence", 0.0)
            self._write("")
            self._write(self._theme.paint("ok", "ANSWER"))
            for chunk in text.splitlines() or [""]:
                self._write(f"  {chunk}")
            self._write(self._theme.paint("muted", f"  confidence={float(confidence):.2f}"))
        else:
            code = envelope.payload.get("code", "ERROR")
            message = envelope.payload.get("message", "")
            self._write("")
            self._write(self._theme.paint("err", str(code)))
            self._write(f"  {message}")
        self._write(self._theme.paint("muted", f"ledger: {ledger_path}"))

    def ask_consent(self, question: str) -> bool:
        """就一次外发升级征求用户同意（ADR-021）。

        前置条件：question 已包含失败摘要与脱敏 payload 预览。
        后置条件：allow 模式直接 True；deny 模式直接 False；
            ask 模式仅在两端都是 TTY 时读入 y/yes，否则默认拒绝。
        禁止事项：不要在非交互环境自动同意；不要把 question 改写后再问。
        异常：无（输入错误按拒绝处理）。
        """
        if self._consent_mode == "allow":
            return True
        if self._consent_mode == "deny":
            self._write(self._theme.paint("warn", "consent: denied by --consent deny"))
            return False
        if not self._interactive():
            self._write(
                self._theme.paint(
                    "warn",
                    "consent: non-interactive environment; default deny (use --consent allow to override)",
                )
            )
            return False

        for line in question.splitlines() or [""]:
            self._write(line)
        self._write_prompt("Allow external escalation? [y/N] ")
        try:
            answer = self._keyboard.readline()
        except (OSError, ValueError):
            return False
        return answer.strip().casefold() in {"y", "yes"}

    def _interactive(self) -> bool:
        """判断是否具备真正的交互条件。

        前置条件：无。
        后置条件：stream 与 keyboard 都有 isatty 且返回 True 时为 True。
        禁止事项：不要把“不是 --json”当作可交互（管道输入同样是非交互）。
        异常：无。
        """
        stream_tty = getattr(self._stream, "isatty", lambda: False)()
        keyboard_tty = getattr(self._keyboard, "isatty", lambda: False)()
        return bool(stream_tty and keyboard_tty)

    def _write_prompt(self, text: str) -> None:
        """写一段不换行的提示文本并 flush。

        前置条件：text 不含换行。
        后置条件：已尝试写出；失败时不抛异常。
        禁止事项：不要在此追加换行（换行由用户输入后的输出负责）。
        异常：无。
        """
        try:
            self._stream.write(text)
            self._stream.flush()
        except (BrokenPipeError, ValueError):
            pass

    def _write(self, text: str) -> None:
        """写一行并 flush，忽略管道关闭。

        前置条件：text 不含尾随换行。
        后置条件：已尝试写出；失败时不抛异常。
        禁止事项：不要在此做格式化（格式化属于 presenter）。
        异常：无。
        """
        try:
            self._stream.write(text + "\n")
            self._stream.flush()
        except (BrokenPipeError, ValueError):
            # 下游（head/管道）已关闭时，继续跑完 turn 以保住 ledger 更合理。
            pass
