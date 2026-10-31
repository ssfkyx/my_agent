"""theme — 外观主题：颜色 token 到 ANSI 的映射。

Module: agent.ui.theme
Responsibility: 提供 token→ANSI 的调色板，并决定是否着色（TTY / NO_COLOR）。
Depends on: stdlib (dataclasses, os, typing)
Depended by: ui.console, (后续) ui.web 的同名 token 表
ADRs: ADR-006（TB-0 只有行式输出）, pre_architecture §9 配色表
"""

import os
import sys
from dataclasses import dataclass
from typing import IO, Mapping

_RESET = "\x1b[0m"

# token 名与 docs/pre_architecture.md §9 的配色表一一对应，Web 阶段复用同一组名字。
_PALETTE: Mapping[str, str] = {
    "plan": "\x1b[33m",
    "supervise": "\x1b[36m",
    "ok": "\x1b[32m",
    "warn": "\x1b[33m",
    "err": "\x1b[31m",
    "review": "\x1b[92m",
    "memory": "\x1b[35m",
    "muted": "\x1b[90m",
    "accent": "\x1b[95m",
}


@dataclass(frozen=True, slots=True)
class Theme:
    """渲染主题。

    前置条件：palette 的键包含渲染器会用到的所有 token。
    后置条件：paint 对未知 token 原样返回文本（不抛异常）。
    禁止事项：禁止在主题里写业务判断（什么颜色代表什么由 presenter 决定）。
    """

    enabled: bool
    palette: Mapping[str, str]

    @classmethod
    def for_stream(cls, stream: IO[str]) -> "Theme":
        """按输出流能力选择是否着色。

        前置条件：stream 具备 isatty 属性（标准输出/错误流）。
        后置条件：非 TTY 或设置了 NO_COLOR 时返回 disabled 主题。
        禁止事项：不要读取 TERM 猜测颜色深度（TB-0 只用基础 8 色）。
        异常：无。
        """
        enabled = bool(getattr(stream, "isatty", lambda: False)()) and not os.environ.get("NO_COLOR")
        return cls(enabled=enabled, palette=_PALETTE)

    def paint(self, token: str, text: str) -> str:
        """给文本套上 token 对应的颜色。

        前置条件：token 可为任意字符串。
        后置条件：enabled 且 token 已知时返回带 ANSI 前缀与重置后缀的文本，否则原样返回。
        禁止事项：不要嵌套调用（会产生颜色泄漏）。
        异常：无。
        """
        if not self.enabled:
            return text
        prefix = self.palette.get(token)
        if prefix is None:
            return text
        return f"{prefix}{text}{_RESET}"


def default_stream() -> IO[str]:
    """返回默认输出流（stdout）。

    前置条件：无。
    后置条件：返回 sys.stdout。
    禁止事项：不要在导入期缓存流对象（测试会替换 stdout）。
    异常：无。
    """
    return sys.stdout
