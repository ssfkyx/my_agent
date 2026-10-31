"""__main__ — 命令行入口（python -m agent）。

Module: agent.__main__
Responsibility: 解析参数、构造 App、执行单轮或 TUI/Web 多轮会话，把结果与退出码翻译给 shell。
Depends on: stdlib (argparse, json, sys, pathlib) + agent.app + agent.config + agent.core.protocol
Depended by: 用户/脚本
ADRs: ADR-006（TB-0 只有 --once）, ADR-002（模型失败要给出可读错误而非堆栈）,
    ADR-021（--consent 决定升级是否外发）, ADR-003（stdlib curses TUI）,
    ADR-027（stdlib HTTP/SSE Web UI）, ADR-042（可选模型启动项）
"""

import argparse
import json
import signal
import sys
from contextlib import contextmanager
from pathlib import Path

from agent.adapters.llama_cpp import ModelBusyError
from agent.app import App
from agent.config import AppConfig, ConfigError, load
from agent.core.evolution import build_evolution_report, format_evolution_report
from agent.core.protocol import MessageKind

_EXIT_OK = 0
_EXIT_TURN_FAILED = 1
_EXIT_ENVIRONMENT = 3
_EXIT_INTERRUPTED = 130


@contextmanager
def _sigterm_as_keyboard_interrupt():
    """把 SIGTERM 转成 KeyboardInterrupt，保证 finally 中的 app.close() 执行。

    前置条件：在主线程中使用（signal.signal 的限制）。
    后置条件：yield 一个可变状态；收到 SIGTERM 时 state["signaled"] 为 True。
    禁止事项：不要吞掉 KeyboardInterrupt；调用方仍需 finally 释放模型进程。
    异常：ValueError（非主线程注册 handler 时由 signal 抛出）。
    """
    previous = signal.getsignal(signal.SIGTERM)
    state = {"signaled": False}

    def _terminate(_signum, _frame):
        state["signaled"] = True
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _terminate)
    try:
        yield state
    finally:
        signal.signal(signal.SIGTERM, previous)


def _parse_model_options(values: list[str], disabled_values: list[str]) -> tuple[dict[str, str], tuple[str, ...]]:
    """解析 `--model ALIAS=PATH` 与 `--disable ALIAS`。"""
    overrides: dict[str, str] = {}
    disabled = {str(alias).strip() for alias in disabled_values if str(alias).strip()}
    for raw in values:
        alias, separator, value = raw.partition("=")
        alias = alias.strip()
        value = value.strip()
        if not separator or not alias or not value:
            raise ValueError(f"--model must be ALIAS=PATH, got: {raw!r}")
        if value.casefold() in {"none", "off", "disabled"}:
            disabled.add(alias)
        else:
            overrides[alias] = value
    return overrides, tuple(sorted(disabled))


def _model_inventory(config: AppConfig) -> dict[str, object]:
    """生成启动模型清单；不启动任何模型。"""
    required = {config.main_alias, config.knowledge.embed_alias}
    role_by_alias: dict[str, list[str]] = {}
    for role, alias in (
        ("main", config.main_alias),
        ("review", config.review_alias),
        ("embed", config.knowledge.embed_alias),
        ("asr", config.media.asr_alias),
        ("vl", config.media.vl_alias),
    ):
        if alias:
            role_by_alias.setdefault(alias, []).append(role)
    rows = []
    for alias, profile in sorted(config.models.items()):
        rows.append(
            {
                "alias": alias,
                "required": alias in required,
                "roles": role_by_alias.get(alias, []),
                "gguf": str(profile.gguf),
                "exists": profile.gguf.is_file(),
                "port": profile.port,
                "embeddings": profile.embeddings,
                "mmproj": None if profile.mmproj is None else str(profile.mmproj),
            }
        )
    return {
        "models": rows,
        "main_alias": config.main_alias,
        "review_alias": config.review_alias or None,
        "embed_alias": config.knowledge.embed_alias,
        "asr_alias": config.media.asr_alias or None,
        "vl_alias": config.media.vl_alias or None,
    }


def _format_model_inventory(inventory: dict[str, object]) -> str:
    lines = [
        f"main:   {inventory.get('main_alias')}",
        f"embed:  {inventory.get('embed_alias')} (required)",
        f"review: {inventory.get('review_alias') or 'disabled'}",
        f"asr:    {inventory.get('asr_alias') or 'disabled'}",
        f"vl:     {inventory.get('vl_alias') or 'disabled'}",
        "",
        "alias        req  roles             exists  port  gguf",
        "---------------------------------------------------------------",
    ]
    for row in inventory.get("models", []):
        roles = ",".join(row.get("roles", [])) or "-"
        lines.append(
            f"{row.get('alias'):<12} {'yes' if row.get('required') else 'no ':<4} "
            f"{roles:<17} {'yes' if row.get('exists') else 'no ':<6} "
            f"{row.get('port'):<5} {row.get('gguf')}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """程序入口。

    前置条件：argv 为 None 时取 sys.argv[1:]。必须选 --once、--tui、--web、--evolve 或 --list-models 之一。
    后置条件：返回退出码：0=ANSWER/正常退出，1=turn 失败（ERROR 信封），3=配置或运行环境不可用。
    禁止事项：不要在这里写编排逻辑（App/Harness 的职责）；不要吞掉退出码。
    异常：无（预期错误均转为可读信息 + 退出码）。
    """
    parser = argparse.ArgumentParser(
        prog="agent",
        description="Run the local agent once (--once), as a curses TUI (--tui), or as a Web UI (--web).",
    )
    parser.add_argument("--once", metavar="TEXT", default=None, help="user request to process in a single turn")
    parser.add_argument("--tui", action="store_true", help="start the curses multi-turn interface")
    parser.add_argument("--web", action="store_true", help="start the HTTP/SSE Web UI on [web].host:[web].port")
    parser.add_argument("--evolve", action="store_true", help="read-only evolution report; never applies proposals")
    parser.add_argument(
        "--model",
        action="append",
        default=[],
        metavar="ALIAS=PATH",
        help="override a model GGUF path; repeatable; use ALIAS=none to disable an optional model",
    )
    parser.add_argument(
        "--disable",
        action="append",
        default=[],
        metavar="ALIAS",
        help="disable an optional model alias (sub/asr/vl/...); repeatable",
    )
    parser.add_argument("--list-models", action="store_true", help="list models, roles, and required/optional status")
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the final envelope as JSON on stdout; supervision lines go to stderr",
    )
    parser.add_argument(
        "--consent",
        choices=("ask", "allow", "deny"),
        default=None,
        help="external escalation consent: ask (TUI/Web prompt), allow, or deny; "
        "default: deny when --json, otherwise ask",
    )
    parser.add_argument("--config", type=Path, default=None, help="path to a TOML config (default: packaged defaults.toml)")
    args = parser.parse_args(argv)

    selected = sum((args.once is not None, args.tui, args.web, args.evolve, args.list_models))
    if selected > 1:
        parser.error("--once, --tui, --web, --evolve and --list-models are mutually exclusive")
    if selected == 0:
        parser.error("one of --once TEXT, --tui, --web, --list-models or --evolve is required")
    try:
        model_overrides, disabled_aliases = _parse_model_options(args.model, args.disable)
    except ValueError as exc:
        parser.error(str(exc))
    if args.json and (args.tui or args.web):
        parser.error("--json cannot be used with --tui or --web")
    if args.tui and not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("tui requires a TTY on stdin and stdout", file=sys.stderr)
        return _EXIT_ENVIRONMENT

    ui_stream = sys.stderr if args.json else sys.stdout
    # --json 视为非交互：默认拒绝外发；要自动化升级必须显式 --consent allow。
    consent_mode = args.consent if args.consent is not None else ("deny" if args.json else "ask")
    ui_mode = "tui" if args.tui else "web" if args.web else "console"
    try:
        config = load(
            args.config,
            model_overrides=model_overrides,
            disabled_aliases=disabled_aliases,
        )
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return _EXIT_ENVIRONMENT

    if args.list_models:
        inventory = _model_inventory(config)
        if args.json:
            print(json.dumps(inventory, ensure_ascii=False, indent=2))
        else:
            print(_format_model_inventory(inventory))
        return _EXIT_OK

    if args.evolve:
        report = build_evolution_report(
            proposals_path=config.ledger_dir / "evolution" / "proposals.jsonl",
            failures_path=config.ledger_dir / "evolution" / "failures.jsonl",
        )
        print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else format_evolution_report(report))
        return _EXIT_OK

    try:
        app = App.build(
            config,
            ui_stream=ui_stream,
            consent_mode=consent_mode,
            ui_mode=ui_mode,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"cannot prepare runtime: {exc}", file=sys.stderr)
        return _EXIT_ENVIRONMENT

    try:
        with _sigterm_as_keyboard_interrupt() as termination:
            if args.tui or args.web:
                if args.web:
                    print(f"web UI: {app.web_url}", file=sys.stderr)
                try:
                    app.run_session()
                except (ModelBusyError, RuntimeError, OSError) as exc:
                    print(f"runtime error: {exc}", file=sys.stderr)
                    return _EXIT_ENVIRONMENT
                finally:
                    app.close()
                return _EXIT_INTERRUPTED if termination["signaled"] else _EXIT_OK

            try:
                outcome = app.run_once(args.once)
            except (ModelBusyError, RuntimeError, OSError) as exc:
                print(f"runtime error: {exc}", file=sys.stderr)
                return _EXIT_ENVIRONMENT
            finally:
                app.close()
    except KeyboardInterrupt:
        # SIGTERM 已在 finally 中完成 app.close()；按 128+15 的惯例返回。
        return _EXIT_INTERRUPTED

    if args.json:
        print(json.dumps(outcome.answer.to_json(), ensure_ascii=False, indent=2))
    return _EXIT_OK if outcome.answer.kind is MessageKind.ANSWER else _EXIT_TURN_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
