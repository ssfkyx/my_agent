"""app — 组装根：把配置、适配器、工具、UI 装成一个可运行的 agent。

Module: agent.app
Responsibility: 唯一允许 new 出具体实现（ServerSupervisor/LlamaCppBrain/ConsoleUI/TuiUI）的地方；
    负责目录创建、资源释放、单轮入口与 TUI 多轮会话。
Depends on: agent.config, agent.adapters.llama_cpp, agent.tools.*, agent.ui.*, agent.core.harness
Depended by: agent.__main__
ADRs: ADR-002（模型生命周期归 supervisor，close 必须有）, ADR-006（TB-0 单轮）,
    ADR-023（RoleRegistry 同时注入 router / harness / switch_role 工具）, ADR-042（可选模型）
"""

import os
import signal
import sys
import threading
from pathlib import Path
from typing import IO

from agent.adapters.embeddings import LlamaCppEmbedder
from agent.adapters.handle_to_master import HttpMaster
from agent.adapters.llama_cpp import ModelRouter, RoleRegistry, ServerSupervisor
from agent.adapters.media import LlamaCppMedia
from agent.adapters.memory import JsonlMemoryStore
from agent.adapters.opinions import OpinionLog
from agent.adapters.sqlite_vectors import SqliteVectorStore
from agent.config import AppConfig
from agent.core.escalation import Escalator
from agent.core.evolution import EvolutionStore
from agent.core.harness import Harness, TurnOutcome
from agent.core.learning import Reviewer
from agent.core.policy import Guardrail
from agent.core.reflection import Reflector
from agent.core.report import Reporter
from agent.knowledge.base import KnowledgeBase
from agent.knowledge.markdown import Chunker
from agent.ports import InteractiveUiPort, UiPort
from agent.tools.delegate import DelegateDraftTool, DelegateReviewTool, SwitchRoleTool
from agent.tools.evolution import ProposeChangeTool
from agent.tools.knowledge import build_tools as build_knowledge_tools
from agent.tools.memory import RecallTool, RecordTool, RequestToolChangeTool
from agent.tools.perception import build_tools as build_perception_tools
from agent.tools.registry import ToolRegistry
from agent.tools.workspace import EchoTool, ListDirTool, ReadFileTool, RunCommandTool, WriteFileTool
from agent.ui.console import ConsoleUI
from agent.ui.presenter import Presenter
from agent.ui.theme import Theme


class App:
    """一次进程的组装根。

    前置条件：config 已通过校验。
    后置条件：构造后即可 run_once；必须成对调用 close。
    禁止事项：禁止在 App 里写业务编排（那属于 Harness）。
    """

    def __init__(
        self,
        config: AppConfig,
        *,
        ui_stream: IO[str] | None = None,
        consent_mode: str = "ask",
        ui_mode: str = "console",
    ) -> None:
        if ui_mode not in {"console", "tui", "web"}:
            raise ValueError("ui_mode must be one of: console, tui, web")
        self._config = config
        self._ui_mode = ui_mode
        self._cancel = threading.Event()
        self._ensure_dirs()
        self._supervisor = ServerSupervisor(
            binary=config.llama_binary,
            profiles=config.models,
            lock_path=config.lock_path,
            log_dir=config.log_dir,
            health_timeout_s=config.health_timeout_s,
        )
        role_map = {"main": config.main_alias}
        if config.review_alias:
            role_map["review"] = config.review_alias
        roles = RoleRegistry(role_map)
        brain = ModelRouter(
            supervisor=self._supervisor,
            registry=roles,
            timeout_s=config.brain_timeout_s,
        )
        self._ui = self._build_ui(ui_stream if ui_stream is not None else sys.stdout, consent_mode)
        knowledge = KnowledgeBase(
            embedder=LlamaCppEmbedder(
                supervisor=self._supervisor,
                alias=config.knowledge.embed_alias,
                timeout_s=config.brain_timeout_s,
            ),
            store=SqliteVectorStore(config.knowledge.db_path),
            chunker=Chunker(max_chars=config.knowledge.chunk_chars),
            candidate_k=config.knowledge.candidate_k,
            parent_chars=config.knowledge.parent_chars,
            hybrid=config.knowledge.hybrid,
            rrf_k=config.knowledge.rrf_k,
            exclude_globs=config.knowledge.exclude_globs,
        )
        memory = JsonlMemoryStore(path=config.memory.path, requests_path=config.memory.requests_path)
        media = None
        if config.media.asr_alias or config.media.vl_alias:
            media = LlamaCppMedia(
                supervisor=self._supervisor,
                profiles=config.models,
                asr_alias=config.media.asr_alias,
                vl_alias=config.media.vl_alias,
                timeout_s=config.brain_timeout_s,
                max_audio_bytes=config.media.max_audio_bytes,
                max_image_bytes=config.media.max_image_bytes,
            )
        escalator = Escalator(master=HttpMaster.from_env())
        evolution_store = EvolutionStore(config.ledger_dir / "evolution" / "proposals.jsonl")
        opinions = OpinionLog(config.workspace_root / "Agents_opinion")
        tools = ToolRegistry(
            [
                EchoTool(),
                ListDirTool(max_entries=config.workspace_limits.max_list_entries),
                ReadFileTool(max_bytes=config.workspace_limits.max_read_bytes),
                WriteFileTool(max_bytes=config.workspace_limits.max_write_bytes),
                RunCommandTool(
                    root=config.workspace_root,
                    allowlist=config.workspace_limits.command_allowlist,
                    timeout_s=config.workspace_limits.command_timeout_s,
                    max_output_bytes=config.workspace_limits.max_command_output_bytes,
                    write_root=config.workspace_limits.write_root,
                    sandbox=config.workspace_limits.command_sandbox,
                    profiles=dict(config.workspace_limits.command_profiles),
                ),
                *build_knowledge_tools(
                    knowledge,
                    default_k=config.knowledge.top_k,
                    default_docs_root=config.knowledge.docs_root,
                ),
                *(
                    build_perception_tools(
                        media,
                        enable_asr=bool(config.media.asr_alias),
                        enable_vl=bool(config.media.vl_alias),
                    )
                    if media is not None
                    else []
                ),
                *(
                    [
                        SwitchRoleTool(roles=roles),
                        DelegateDraftTool(brain=brain, roles=roles),
                        DelegateReviewTool(brain=brain, roles=roles),
                    ]
                    if config.review_alias
                    else []
                ),
                RecallTool(memory=memory, default_limit=config.memory.recall_limit),
                RecordTool(memory=memory),
                RequestToolChangeTool(memory=memory),
                ProposeChangeTool(store=evolution_store, root=config.workspace_root, opinions=opinions),
            ]
        )
        reviewer = (
            Reviewer(
                brain=brain,
                temperature=config.policy.temperature,
                max_tokens=config.review_max_tokens,
            )
            if config.review_alias
            else None
        )
        reflector = (
            Reflector(
                brain=brain,
                temperature=config.policy.temperature,
                max_tokens=config.review_max_tokens,
            )
            if config.review_alias
            else None
        )
        self._harness = Harness(
            brain=brain,
            tools=tools,
            ui=self._ui,
            policy=config.policy,
            guardrail=Guardrail(config.guardrail),
            reviewer=reviewer,
            reporter=Reporter(),
            reflector=reflector,
            memory=memory,
            recall_limit=config.memory.recall_limit,
            ledger_dir=config.ledger_dir,
            escalator=escalator,
            roles=roles,
            runtime=self._supervisor,
        )

    @classmethod
    def build(
        cls,
        config: AppConfig,
        *,
        ui_stream: IO[str] | None = None,
        consent_mode: str = "ask",
        ui_mode: str = "console",
    ) -> "App":
        """构造 App，并允许把 UI 输出重定向（--json 时走 stderr）。

        前置条件：config 合法；consent_mode ∈ {ask, allow, deny}；ui_mode ∈ {console, tui}。
        后置条件：返回已装配的 App；目录已创建；UI 只构建一次。
        禁止事项：不要在 build 中拉起模型（加载 9 GB 权重应由 run_once 决定）。
        异常：OSError（目录不可创建）、ValueError（consent_mode/ui_mode 非法）。
        """
        return cls(
            config,
            ui_stream=ui_stream,
            consent_mode=consent_mode,
            ui_mode=ui_mode,
        )

    def run_once(self, user_text: str, *, cancel: threading.Event | None = None) -> TurnOutcome:
        """执行一轮：模型驻留由 ModelRouter 按 role 按需拉起（ADR-014）。

        前置条件：user_text 非空；cancel 可选（协作式取消）。
        后置条件：返回 outcome；最后驻留的模型可能是 review 角色（由 close 释放）。
        禁止事项：不要在 run_once 内并行处理第二个输入。
        异常：ModelBusyError、RuntimeError（模型启动失败）、OSError（ledger/报告写入失败）。
        """
        return self._harness.run_turn(user_text, cancel=cancel)

    def request_cancel(self) -> None:
        """请求取消当前 turn（Web /cancel 或 TUI Ctrl-C 调用）。"""
        self._cancel.set()

    def run_session(self) -> None:
        """运行多轮交互会话，直到用户退出。

        前置条件：UI 实现 InteractiveUiPort（tui/web）；当前不在 turn 中。
        后置条件：每轮输入走 run_once；单轮运行错误显示在 UI 后继续下一轮。
        禁止事项：不要在 console UI 上调用（没有 read_user_input）。
        异常：RuntimeError（UI 不支持交互）、KeyboardInterrupt（按退出处理）。
        """
        if not isinstance(self._ui, InteractiveUiPort):
            raise RuntimeError("run_session requires an interactive UI (--tui/--web)")
        if self._ui_mode == "web":
            self._run_web_sessions()
            return
        try:
            while True:
                text = self._ui.read_user_input()
                if text is None:
                    break
                text = text.strip()
                if not text:
                    continue
                if text.casefold() in {"/quit", "/exit", "quit", "exit"}:
                    break
                self._cancel.clear()
                try:
                    if self._ui_mode == "tui":
                        previous = signal.getsignal(signal.SIGINT)
                        signal.signal(signal.SIGINT, lambda _signum, _frame: self._cancel.set())
                        try:
                            self.run_once(text, cancel=self._cancel)
                        finally:
                            signal.signal(signal.SIGINT, previous)
                    else:
                        self.run_once(text, cancel=self._cancel)
                except (RuntimeError, OSError) as exc:
                    self._ui.show_error(f"{type(exc).__name__}: {exc}")
        except KeyboardInterrupt:
            pass

    def _run_web_sessions(self) -> None:
        """Web 多会话循环：会话隔离，但 turn 全局串行。

        前置条件：UI 提供 next_job/begin_turn/end_turn/close_session（WebUI）。
        后置条件：每次只执行一个 turn；某个会话 /quit 只关闭该会话。
        禁止事项：不要在这里并行调用 run_once；模型调用必须串行。
        异常：无（单轮错误显示在对应会话）。
        """
        ui = self._ui
        try:
            while True:
                job = ui.next_job()  # type: ignore[attr-defined]
                if job is None:
                    break
                sid, text = job
                text = text.strip()
                if not text:
                    ui.end_turn(sid)  # type: ignore[attr-defined]
                    continue
                if text.casefold() in {"/quit", "/exit", "quit", "exit"}:
                    ui.close_session(sid)  # type: ignore[attr-defined]
                    continue
                self._cancel.clear()
                ui.begin_turn(sid)  # type: ignore[attr-defined]
                try:
                    self.run_once(text, cancel=self._cancel)
                except (RuntimeError, OSError) as exc:
                    ui.show_error(f"{type(exc).__name__}: {exc}")
                finally:
                    ui.end_turn(sid)  # type: ignore[attr-defined]
        except KeyboardInterrupt:
            pass

    def close(self) -> None:
        """释放模型进程、文件锁与终端状态。

        前置条件：无。
        后置条件：无残留 llama-server 子进程；锁已释放；TUI 已 endwin；可重复调用。
        禁止事项：不要在 close 后继续 run_once。
        异常：无。
        """
        try:
            self._supervisor.close()
        finally:
            close = getattr(self._ui, "close", None)
            if callable(close):
                close()

    def _build_ui(self, stream: IO[str], consent_mode: str) -> UiPort:
        """按 ui_mode 构造控制台、curses 或 Web UI。

        前置条件：stream 可写；consent_mode ∈ {ask, allow, deny}。
        后置条件：console → ConsoleUI；tui → TuiUI；web → 已启动 HTTP 服务的 WebUI。
        禁止事项：不要在 UI 中注入业务依赖。
        异常：ValueError（参数非法）、curses.error（终端不可用）、OSError（Web 端口占用）。
        """
        if self._ui_mode == "tui":
            from agent.ui.tui import TuiUI

            return TuiUI(consent_mode=consent_mode)
        if self._ui_mode == "web":
            from agent.ui.web import WebUI

            token_env = self._config.web.auth_token_env
            token = os.environ.get(token_env, "").strip() if token_env else ""
            return WebUI(
                host=self._config.web.host,
                port=self._config.web.port,
                consent_mode=consent_mode,
                auth_token=token or None,
                tls_cert=self._config.web.tls_cert,
                tls_key=self._config.web.tls_key,
                on_cancel=self.request_cancel,
                upload_root=self._config.workspace_limits.write_root,
                workspace_root=self._config.workspace_root,
                max_image_bytes=self._config.media.max_image_bytes,
                max_audio_bytes=self._config.media.max_audio_bytes,
                media_enabled=bool(
                    self._config.media.asr_alias or self._config.media.vl_alias
                ),
                image_enabled=bool(self._config.media.vl_alias),
                audio_enabled=bool(self._config.media.asr_alias),
            )
        return ConsoleUI(
            presenter=Presenter(),
            theme=Theme.for_stream(stream),
            stream=stream,
            consent_mode=consent_mode,
        )

    @property
    def web_url(self) -> str | None:
        """web 模式返回浏览器访问地址，其余模式返回 None。"""
        if self._ui_mode != "web":
            return None
        return getattr(self._ui, "url", None)

    def _ensure_dirs(self) -> None:
        """创建运行期目录。

        前置条件：config 中的路径均已绝对化。
        后置条件：ledger_dir、log_dir、lock 文件父目录、write_root 存在。
        禁止事项：不要创建模型目录（模型由用户放置）。
        异常：OSError（权限不足）。
        """
        paths = [self._config.ledger_dir, self._config.log_dir, self._config.lock_path.parent]
        if self._config.workspace_limits.write_root is not None:
            paths.append(self._config.workspace_limits.write_root)
        for path in paths:
            Path(path).mkdir(parents=True, exist_ok=True)
