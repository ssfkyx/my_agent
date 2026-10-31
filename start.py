#!/usr/bin/env python3
"""start — 双击启动器（默认 Web UI）。

直接运行：

    python3 start.py

双击：

    Windows: start.bat
    Linux:   start.sh
    macOS:   start.command

默认启动 `python -m agent --web` 并打开浏览器；其他参数原样传给 agent。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import threading
import tomllib
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
_DEFAULT_CONFIG = ROOT / "agent" / "config" / "defaults.toml"


def _agent_args(raw: list[str]) -> list[str]:
    """没有参数时默认 --web；有参数时原样传递。"""
    if not raw:
        return ["--web"]
    if raw[0] == "--":
        raw = raw[1:]
    return raw or ["--web"]


def _config_path(agent_args: list[str]) -> Path:
    for index, arg in enumerate(agent_args):
        if arg == "--config" and index + 1 < len(agent_args):
            candidate = Path(agent_args[index + 1]).expanduser()
            return candidate if candidate.is_absolute() else ROOT / candidate
        if arg.startswith("--config="):
            candidate = Path(arg.split("=", 1)[1]).expanduser()
            return candidate if candidate.is_absolute() else ROOT / candidate
    return _DEFAULT_CONFIG


def _web_url(agent_args: list[str]) -> str:
    """从 TOML 读取 web host/port；读取失败时回退到 127.0.0.1:8765。"""
    try:
        raw = tomllib.loads(_config_path(agent_args).read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return "http://127.0.0.1:8765/"
    web = raw.get("web", {})
    if not isinstance(web, dict):
        return "http://127.0.0.1:8765/"
    host = str(web.get("host", "127.0.0.1"))
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    try:
        port = int(web.get("port", 8765))
    except (TypeError, ValueError):
        port = 8765
    scheme = "https" if web.get("tls_cert") else "http"
    return f"{scheme}://{host}:{port}/"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="start",
        description="Double-click launcher for the local agent (defaults to --web).",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="start Web UI but do not open the browser automatically",
    )
    args, passthrough = parser.parse_known_args(argv)
    agent_args = _agent_args(passthrough)
    command = [sys.executable, "-m", "agent", *agent_args]

    print("starting:", " ".join(command))
    print("cwd:", ROOT)
    try:
        process = subprocess.Popen(command, cwd=ROOT)
    except OSError as exc:
        print(f"cannot start agent: {exc}", file=sys.stderr)
        return 3

    if "--web" in agent_args and not args.no_browser:
        url = _web_url(agent_args)
        timer = threading.Timer(1.5, lambda: webbrowser.open(url))
        timer.daemon = True
        timer.start()
        print(f"web UI: {url}")

    try:
        return int(process.wait())
    except KeyboardInterrupt:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
