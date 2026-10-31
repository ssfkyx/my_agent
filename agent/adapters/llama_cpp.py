"""llama_cpp — llama.cpp 适配器：单常驻模型管理与 BrainPort 实现。

Module: agent.adapters.llama_cpp
Responsibility: 拉起/停止唯一的 llama-server（ServerSupervisor），以 OpenAI 兼容 HTTP 实现
    BrainPort（LlamaCppBrain），并按 role 路由到对应模型（ModelRouter）。
Depends on: stdlib (fcntl, json, os, signal, subprocess, time, urllib, pathlib) + agent.ports + agent.config
Depended by: agent.app
ADRs: ADR-002（单实例 + 文件锁 + --fit）, ADR-003（FP64 计时、零依赖 HTTP）,
    ADR-004（BrainPort 契约）, ADR-014（role 路由）, ADR-023（RoleRegistry 当前角色）,
    ADR-033（Q4 主脑）, ADR-034（ngl 降档重试）
"""

import fcntl
import json
import os
import signal
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, IO, Mapping

from agent.config import ModelProfile
from agent.ports import BrainError, BrainReply, BrainRequest, RoleError, RolePort


class ModelBusyError(RuntimeError):
    """无法独占模型：已有本进程之外的 supervisor 持锁，或端口被别的服务占用。

    前置条件：无。
    后置条件：异常信息说明冲突来源（锁文件或端口）。
    禁止事项：不要自动杀别人的进程（可能正跑着别的任务）。
    异常来源：ServerSupervisor.ensure。
    """


def _escalate(
    proc: subprocess.Popen[bytes],
    grace_s: float,
    soft,
    hard,
) -> None:
    """先温和终止、超时再强制终止；进程已消失不是错误。

    前置条件：proc 可能已经退出（幂等）。
    后置条件：soft/hard 都已尝试；不向调用方抛进程查找类异常。
    禁止事项：不要在这里无限等待（调用方需要尽快释放显存并尝试下一档）。
    异常：无（预期竞态均被吞掉）。
    """
    try:
        soft()
        proc.wait(timeout=grace_s)
        return
    except (ProcessLookupError, PermissionError):
        return
    except (subprocess.TimeoutExpired, OSError):
        pass
    try:
        hard()
        proc.wait(timeout=grace_s)
    except (ProcessLookupError, PermissionError, subprocess.TimeoutExpired, OSError):
        pass


class ServerSupervisor:
    """全进程唯一的 llama-server 生命周期管理者（ADR-002）。

    前置条件：binary 可执行；profiles 的端口互不重复（config 已校验）。
    后置条件：同一时刻最多一个由本对象拉起的服务进程；close 后不残留子进程。
    禁止事项：禁止绕过 ensure 直接 Popen；禁止并发调用 ensure。
    """

    def __init__(
        self,
        *,
        binary: Path,
        profiles: Mapping[str, ModelProfile],
        lock_path: Path,
        log_dir: Path,
        health_timeout_s: float,
    ) -> None:
        self._binary = binary
        self._profiles = dict(profiles)
        self._lock_path = lock_path
        self._log_dir = log_dir
        self._health_timeout_s = health_timeout_s
        self._process: subprocess.Popen[bytes] | None = None
        self._log_handle: IO[bytes] | None = None
        self._alias: str | None = None
        self._ngl_used: int | None = None
        # 本进程内成功拉起过的 alias → 实测 ngl（诊断用）。
        self._loaded: dict[str, int | None] = {}
        # 本轮内被 ensure 过的 alias → 实测 ngl；RunReport 只读这一份。
        self._turn_used: dict[str, int | None] = {}
        self._lock_handle: IO[str] | None = None

    @property
    def resident(self) -> str | None:
        """当前驻留模型的 alias（无则为 None）。

        前置条件：无。
        后置条件：返回 alias 或 None，不触发任何副作用。
        禁止事项：不要用它判断模型是否就绪（健康检查用 ensure 的返回）。
        异常：无。
        """
        return self._alias

    @property
    def gpu_layers(self) -> int | None:
        """当前驻留模型实际使用的 ngl（None 表示 --fit on）。"""
        return self._ngl_used

    def begin_turn(self) -> None:
        """开始新一轮统计：清空本轮使用过的模型集合（ADR-034）。

        前置条件：无。
        后置条件：`snapshot()` 只会包含此后 ensure 过的模型。
        禁止事项：不要在 turn 进行中调用（会丢掉已记录的本轮模型）。
        异常：无。
        """
        self._turn_used.clear()

    def _record_use(self, alias: str) -> None:
        """记录本轮使用过的 alias 及其当前实测 ngl。"""
        self._turn_used[alias] = self._ngl_used

    def snapshot(self) -> dict[str, int | None]:
        """返回本轮 ensure 过的模型及其实测 ngl（ADR-034）。

        前置条件：Harness 已在本轮开始时调用 begin_turn()。
        后置条件：返回副本；不触发 ensure/health 等副作用。
        禁止事项：不要为本轮未使用的模型编造 ngl。
        异常：无。
        """
        return dict(self._turn_used)

    def ensure(self, alias: str) -> str:
        """确保指定模型驻留，并返回其 base_url。

        前置条件：alias 已在配置中定义；调用方已完成其他模型调用的收尾。
        后置条件：目标模型健康可服务；返回 http://host:port。
        禁止事项：不要在目标模型已驻留时重启它（会白白付出加载时间）。
        异常：ModelBusyError（锁被占/端口被外部占用）、RuntimeError（启动失败或超时）。
        """
        try:
            profile = self._profiles[alias]
        except KeyError as exc:
            raise ModelBusyError(f"unknown model alias: {alias}") from exc

        self._acquire_lock()
        if self._alias == alias and self._is_alive():
            self._record_use(alias)
            return profile.base_url

        # 先停旧的再起新的：显存只够一个模型，顺序不能反。
        self.stop()
        self._start(profile)
        self._alias = alias
        self._record_use(alias)
        return profile.base_url

    def stop(self) -> None:
        """停止当前模型并释放显存。

        前置条件：无（可重复调用）。
        后置条件：子进程已退出、日志句柄已关闭、resident 为 None。
        禁止事项：不要在 stop 中释放文件锁（锁属于 supervisor 生命周期）。
        异常：无（终止失败会被强制 kill）。
        """
        proc = self._process
        self._process = None
        self._alias = None
        self._ngl_used = None
        if proc is not None:
            self._terminate_process_tree(proc, 15.0)
        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None

    @staticmethod
    def _terminate_process_tree(proc: subprocess.Popen[bytes], grace_s: float) -> None:
        """终止 llama-server 及其派生进程，避免孤儿继续占显存。

        前置条件：proc 由本类以 start_new_session=True 拉起（或已退出）。
        后置条件：进程组收到 SIGTERM；超时后收到 SIGKILL；幂等。
        禁止事项：不要在 pgid 等于自身进程组时调用 killpg（会连自己一起杀掉）；
            此时退化为只终止 proc 本身。
        异常：无（进程竞态按已退出处理）。
        """
        if proc.poll() is not None:
            return
        try:
            pgid = os.getpgid(proc.pid)
        except (ProcessLookupError, PermissionError, OSError):
            pgid = None
        if pgid is None or pgid == os.getpgrp():
            _escalate(proc, grace_s, proc.terminate, proc.kill)
            return
        _escalate(
            proc,
            grace_s,
            lambda: os.killpg(pgid, signal.SIGTERM),
            lambda: os.killpg(pgid, signal.SIGKILL),
        )

    def close(self) -> None:
        """停止模型并释放文件锁。

        前置条件：无。
        后置条件：本对象不再持有任何进程或锁；可安全重复调用。
        禁止事项：不要在 close 之后继续 ensure（会重新抢锁，语义不明确）。
        异常：无。
        """
        self.stop()
        if self._lock_handle is not None:
            try:
                fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_UN)
            finally:
                self._lock_handle.close()
                self._lock_handle = None

    def _acquire_lock(self) -> None:
        """以非阻塞方式抢占全局模型锁。

        前置条件：lock_path 的父目录可写。
        后置条件：成功时本对象持有排他锁；重复调用直接返回。
        禁止事项：不要用阻塞等待（第二个实例应立刻失败，而不是排队到 OOM）。
        异常：ModelBusyError（已有其他实例持锁）。
        """
        if self._lock_handle is not None:
            return
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = self._lock_path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise ModelBusyError(
                f"another agent instance holds {self._lock_path}; refusing to load a second model"
            ) from exc
        self._lock_handle = handle

    def _start(self, profile: ModelProfile) -> None:
        """启动并等待模型就绪；失败时按 ngl 阶梯降档重试（ADR-034）。

        前置条件：当前无驻留进程；端口未被外部服务占用。
        后置条件：llama-server 已通过 /health；日志写入 log_dir/<alias>.log。
        禁止事项：不要在未确认端口空闲时启动（会连到别人的模型上）；
            不要把一次 OOM 当作模型不可用——WSL2 的单块连续分配失败常可通过降档绕过。
        异常：ModelBusyError（端口被非本进程占用）、RuntimeError（所有尝试都失败）。
        """
        if self._health_ok(profile):
            raise ModelBusyError(
                f"{profile.base_url} already answers /health but is not managed by this process; "
                "refuse to attach to an unknown model"
            )

        self._log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self._log_dir / f"{profile.alias}.log"
        attempts = profile.gpu_layers_ladder()
        errors: list[str] = []
        for attempt_index, ngl in enumerate(attempts, 1):
            label = "--fit on" if ngl is None else f"-ngl {ngl}"
            log_handle = log_path.open("ab")
            log_handle.write(
                f"\n=== start attempt {attempt_index}/{len(attempts)}: {label} ===\n".encode(
                    "utf-8"
                )
            )
            log_handle.flush()
            args = self._build_args(profile, ngl)
            try:
                self._process = subprocess.Popen(
                    args,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except OSError as exc:
                log_handle.close()
                raise RuntimeError(f"failed to start llama-server: {exc}") from exc
            self._log_handle = log_handle

            deadline = time.monotonic() + self._health_timeout_s
            while time.monotonic() < deadline:
                if self._process.poll() is not None:
                    tail = self._log_tail(log_path, 20)
                    errors.append(f"{label}: exited early; {tail}")
                    self.stop()
                    break
                if self._health_ok(profile):
                    self._ngl_used = ngl
                    self._loaded[profile.alias] = ngl
                    with log_path.open("a", encoding="utf-8") as ready_log:
                        ready_log.write(
                            f"=== ready: {profile.alias} {label} "
                            f"(attempt {attempt_index}/{len(attempts)}) ===\n"
                        )
                    return
                time.sleep(0.5)
            else:
                tail = self._log_tail(log_path, 20)
                errors.append(f"{label}: not healthy within {self._health_timeout_s}s; {tail}")
                self.stop()
            time.sleep(0.5)

        detail = " | ".join(errors) if errors else "(no attempt)"
        raise RuntimeError(
            f"all ngl attempts failed for {profile.alias} ({', '.join(
                'fit' if ngl is None else str(ngl) for ngl in attempts
            )}); see {log_path}: {detail}"
        )


    def _build_args(self, profile: ModelProfile, ngl: int | None = None) -> list[str]:
        """构造 llama-server 命令行。

        前置条件：profile 已完成 config 校验。
        后置条件：返回参数列表；显式 ngl 优先于 profile.n_gpu_layers。
        禁止事项：不要省略 --fit/-ngl（否则默认值可能整模型塞进显存导致 OOM）；
            不要省略 --parallel 1（默认多 slot 会摊薄 ctx 并增加 KV 分配）。
        异常：无。
        """
        if ngl is None and profile.n_gpu_layers is not None:
            ngl = profile.n_gpu_layers
        args = [
            str(self._binary),
            "-m",
            str(profile.gguf),
            "--host",
            profile.host,
            "--port",
            str(profile.port),
            "-c",
            str(profile.n_ctx),
            "--parallel",
            "1",
            "--no-webui",
        ]
        if profile.mmproj is not None:
            args.extend(["-mm", str(profile.mmproj), "--no-mmproj-auto"])
        if profile.embeddings:
            args.extend(["--embeddings", "--pooling", "last"])
        if ngl is None:
            # 让 llama.cpp 按可用显存自行下调参数：12 GB 卡上这比手算层数稳。
            args.extend(["--fit", "on"])
        else:
            args.extend(["-ngl", str(ngl)])
        return args

    def _is_alive(self) -> bool:
        """判断已驻留进程是否仍在运行。

        前置条件：无。
        后置条件：返回布尔值。
        禁止事项：不要在此做健康检查（会额外发 HTTP 请求）。
        异常：无。
        """
        return self._process is not None and self._process.poll() is None

    def _health_ok(self, profile: ModelProfile) -> bool:
        """查询 /health。

        前置条件：无。
        后置条件：服务返回 status=ok 时为 True，其余情况 False。
        禁止事项：不要在超时后重试放大负载。
        异常：无（网络异常按不可用处理）。
        """
        try:
            with urllib.request.urlopen(f"{profile.base_url}/health", timeout=2.0) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
            return False
        return isinstance(payload, Mapping) and payload.get("status") == "ok"

    @staticmethod
    def _log_tail(path: Path, lines: int) -> str:
        """读取日志末尾若干行用于报错。

        前置条件：path 可能不存在。
        后置条件：返回至多 lines 行文本；不存在时返回空串。
        禁止事项：不要把整份日志塞进异常（可能很长）。
        异常：无。
        """
        try:
            content = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return ""
        return "\n".join(content[-lines:])


class LlamaCppBrain:
    """通过 llama-server 的 OpenAI 兼容接口实现 BrainPort（ADR-004）。

    前置条件：base_url 指向已由 ServerSupervisor.ensure 就绪的服务。
    后置条件：每次 complete 只发一次请求，不做内部重试。
    禁止事项：禁止并发调用（显存单模型，且服务端 KV cache 会被争用）。
    """

    def __init__(self, *, base_url: str, model_alias: str, timeout_s: float) -> None:
        self._base_url = base_url.rstrip("/")
        self._model_alias = model_alias
        self._timeout_s = timeout_s

    def complete(self, request: BrainRequest) -> BrainReply:
        """执行一次 chat/completions 调用。

        前置条件：messages 非空且可 JSON 序列化。
        后置条件：返回 BrainReply；指定 json_schema 时 content 为解析后的 dict。
        禁止事项：不要在客户端修补模型输出（schema 约束由服务端 GBNF 完成）。
        异常：BrainError（网络、HTTP 非 2xx、响应缺字段、JSON 解析失败）。
        """
        body: dict[str, Any] = {
            "model": self._model_alias,
            "messages": [dict(message) for message in request.messages],
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
            "stream": False,
        }
        if request.json_schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "reply", "schema": dict(request.json_schema)},
            }
        if request.disable_thinking:
            # 交给模板处理：不识别 enable_thinking 的模板会忽略该键（ADR-015）。
            body["chat_template_kwargs"] = {"enable_thinking": False}

        started = time.perf_counter()
        raw = self._post("/v1/chat/completions", body)
        elapsed = time.perf_counter() - started

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise BrainError(f"model returned non-JSON envelope: {raw[:200]}") from exc

        choices = parsed.get("choices")
        if not isinstance(choices, list) or not choices:
            raise BrainError(f"response has no choices: {raw[:200]}")
        message = choices[0].get("message") or {}
        content_text = message.get("content")
        if not isinstance(content_text, str) or not content_text.strip():
            raise BrainError("response message.content is empty")

        try:
            content = json.loads(content_text)
        except json.JSONDecodeError as exc:
            raise BrainError(f"model content is not JSON: {content_text[:200]}") from exc
        if not isinstance(content, Mapping):
            raise BrainError("model content must be a JSON object")

        usage = parsed.get("usage") or {}
        tokens = int(usage.get("completion_tokens", 0))
        return BrainReply(content=content, raw=content_text, tokens=tokens, elapsed_s=elapsed)

    def health(self) -> bool:
        """探测服务可用性。

        前置条件：无。
        后置条件：返回布尔值，不抛异常。
        禁止事项：不要用它判断模型质量。
        异常：无。
        """
        try:
            with urllib.request.urlopen(f"{self._base_url}/health", timeout=2.0) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
            return False
        return isinstance(payload, Mapping) and payload.get("status") == "ok"

    def _post(self, path: str, body: Mapping[str, Any]) -> str:
        """发送 JSON POST 并返回响应文本。

        前置条件：body 可被 json.dumps 序列化。
        后置条件：返回 2xx 响应体字符串。
        禁止事项：不要在异常信息里包含请求头或密钥。
        异常：BrainError（HTTP 非 2xx、网络错误、超时）。
        """
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self._base_url}{path}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_s) as response:
                return response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise BrainError(f"HTTP {exc.code} from {path}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise BrainError(f"cannot reach {self._base_url}{path}: {exc}") from exc


class RoleRegistry:
    """角色 → 模型 alias 的登记表，并记住当前主脑角色（ADR-014/ADR-023）。

    前置条件：roles 非空；current 必须是 roles 的键。
    后置条件：resolve 是纯查询；switch_role 只改 current，不启动/停止模型。
    禁止事项：不要在这里调用 supervisor.ensure（加载时机属于下一次模型调用）。
    """

    def __init__(self, roles: Mapping[str, str], *, current: str = "main") -> None:
        if not roles:
            raise ValueError("roles must not be empty")
        if any(not role.strip() or not alias.strip() for role, alias in roles.items()):
            raise ValueError("role and alias must be non-empty")
        self._roles: dict[str, str] = dict(roles)
        if current not in self._roles:
            raise RoleError(f"current role {current!r} is not registered; roles={sorted(self._roles)}")
        self._current = current

    @property
    def current(self) -> str:
        """当前主脑角色名。

        前置条件：构造时已校验 current 合法。
        后置条件：返回上次 switch_role 的结果（默认 main）。
        禁止事项：不要返回 alias 冒充 role（两者语义不同）。
        异常：无。
        """
        return self._current

    def resolve(self, role: str) -> str:
        """把角色解析成模型 alias。

        前置条件：role 为字符串（可为未知值）。
        后置条件：已登记角色返回 alias；未知角色抛 RoleError 并列出已知角色。
        禁止事项：不要对未知角色回落 main。
        异常：RoleError。
        """
        if not isinstance(role, str) or not role.strip():
            raise RoleError(f"role must be a non-empty string; roles={sorted(self._roles)}")
        try:
            return self._roles[role]
        except KeyError as exc:
            raise RoleError(f"unknown role: {role!r}; roles={sorted(self._roles)}") from exc

    def switch_role(self, role: str) -> str:
        """切换当前角色并返回其 alias。

        前置条件：role 为已登记角色。
        后置条件：current == role；驻留的 llama-server 不变（下一次 complete 才换卡）。
        禁止事项：不要在这里新开 Turn、清空 ledger 或调用模型。
        异常：RoleError。
        """
        alias = self.resolve(role)
        self._current = role
        return alias


class ModelRouter:
    """按 role 路由到本地模型（ADR-014）。

    前置条件：registry（或 roles）中的每个 alias 都在 supervisor 的 profiles 中；timeout_s > 0。
    后置条件：同一 alias 的连续调用复用同一个 LlamaCppBrain；跨 alias 会切换驻留模型。
    禁止事项：禁止对未知 role 回落到主脑（会让"评审"悄悄变成"自评"）。
    """

    def __init__(
        self,
        *,
        supervisor: ServerSupervisor,
        timeout_s: float,
        roles: Mapping[str, str] | None = None,
        registry: RolePort | None = None,
    ) -> None:
        if registry is None:
            if roles is None:
                raise ValueError("ModelRouter requires roles= or registry=")
            registry = RoleRegistry(roles)
        self._supervisor = supervisor
        self._registry = registry
        self._timeout_s = timeout_s
        self._brains: dict[str, LlamaCppBrain] = {}

    def complete(self, request: BrainRequest) -> BrainReply:
        """按 role 确保模型驻留并执行一次推理。

        前置条件：request.role 已在 registry 中登记。
        后置条件：返回 BrainReply；必要时已完成一次模型切换（3–8 s）。
        禁止事项：不要在内部并行两次 ensure（单模型，见 ADR-002）。
        异常：RoleError（未知 role）、BrainError（上游失败）、ModelBusyError/RuntimeError（起服务失败）。
        """
        alias = self._registry.resolve(request.role)
        base_url = self._supervisor.ensure(alias)
        brain = self._brains.get(alias)
        if brain is None:
            brain = LlamaCppBrain(base_url=base_url, model_alias=alias, timeout_s=self._timeout_s)
            self._brains[alias] = brain
        return brain.complete(request)

    def health(self) -> bool:
        """探测当前驻留模型是否可用。

        前置条件：无。
        后置条件：有驻留且其 brain 健康时为 True，否则 False。
        禁止事项：不要在无驻留时主动拉起模型（health 不应有副作用）。
        异常：无。
        """
        resident = self._supervisor.resident
        brain = self._brains.get(resident) if resident else None
        return brain.health() if brain is not None else False
