"""config — 配置的加载、校验与模型目录。

Module: agent.config
Responsibility: 把 TOML 读成 frozen 的 AppConfig / ModelProfile，并在启动时校验路径与端口。
Depends on: stdlib (dataclasses, pathlib, tomllib, typing) + agent.core.harness(HarnessPolicy)
Depended by: app, adapters.llama_cpp, __main__
ADRs: ADR-002（模型端口与显存策略）, ADR-003（tomllib 属 stdlib）, ADR-006（TB-0 只用 main）,
    ADR-026（workspace 写/执行策略）, ADR-028（bwrap 命令沙箱）,
    ADR-033（Q4 主脑）, ADR-034（ngl 降档）, ADR-042（可选模型）
"""

import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Mapping

from agent.core.harness import HarnessPolicy
from agent.core.policy import GuardrailPolicy

_DEFAULT_CONFIG = Path(__file__).with_name("config") / "defaults.toml"

# 宿主上已被其他服务占用的端口；禁止任何 agent 子服务使用（2026-10-30 用户约束）。
_RESERVED_PORTS = frozenset({8080, 5117})


class ConfigError(ValueError):
    """配置文件不可用（缺字段、路径不存在、端口冲突）。

    前置条件：无。
    后置条件：异常信息指出出错的键或路径。
    禁止事项：不要在校验失败时回落到硬编码默认值（会把部署漂移藏起来）。
    异常来源：load。
    """


@dataclass(frozen=True, slots=True)
class ModelProfile:
    """一个可被 ServerSupervisor 拉起的本地模型。

    前置条件：gguf 存在；port 在 1..65535；n_ctx >= 512。
    后置条件：base_url 恒定指向 loopback，不对外暴露。
    禁止事项：禁止两个 profile 共用同一端口（会互相抢监听）。
    """

    alias: str
    gguf: Path
    host: str
    port: int
    n_ctx: int
    n_gpu_layers: int | None
    mmproj: Path | None = None
    embeddings: bool = False

    @property
    def base_url(self) -> str:
        """返回该模型的 OpenAI 兼容服务根地址。

        前置条件：无。
        后置条件：形如 http://127.0.0.1:<port>。
        禁止事项：不要在此拼 /v1（调用方各自拼接路径）。
        异常：无。
        """
        return f"http://{self.host}:{self.port}"

    def gpu_layers_ladder(self) -> tuple[int | None, ...]:
        """返回启动尝试的 ngl 阶梯（ADR-034）。

        前置条件：无。
        后置条件：None 表示 `--fit on`；显式值表示 `-ngl N`。
        禁止事项：不要只尝试一次——WSL2 下单块连续分配失败时，降档常常能成功。
        """
        if self.n_gpu_layers is None:
            return (None, 32, 28, 24, 20, 16, 12, 8, 4, 0)
        start = max(0, self.n_gpu_layers)
        steps: list[int | None] = [start]
        for candidate in (32, 28, 24, 20, 16, 12, 8, 4, 0):
            if candidate < start and candidate not in steps:
                steps.append(candidate)
        return tuple(steps)


@dataclass(frozen=True, slots=True)
class WorkspaceLimits:
    """工作区工具的资源上限与写/执行策略（ADR-026）。

    前置条件：字节/条数/超时均 > 0；write_root 绝对或 None；命令前缀非空。
    后置条件：不可变；作为工具与 guardrail 的构造参数注入。
    禁止事项：不要在运行期调整（会让同一计划的输出大小/权限不稳定）。
    """

    max_read_bytes: int
    max_list_entries: int
    write_root: Path | None = None
    max_write_bytes: int = 65536
    command_allowlist: tuple[tuple[str, ...], ...] = ()
    command_timeout_s: float = 15.0
    max_command_output_bytes: int = 65536
    # run_command 的隔离方式：bwrap=强制沙箱；auto=有 bwrap 就用；none=直接执行（ADR-028）。
    command_sandbox: str = "auto"
    # 模型可自选的命令 profile；default 由 command_allowlist 提供（ADR-031）。
    command_profiles: tuple[tuple[str, tuple[tuple[str, ...], ...]], ...] = ()

    def __post_init__(self) -> None:
        if self.max_read_bytes <= 0 or self.max_list_entries <= 0:
            raise ValueError("workspace limits must be positive")
        if self.max_write_bytes <= 0 or self.max_command_output_bytes <= 0:
            raise ValueError("write/output limits must be positive")
        if self.command_timeout_s <= 0:
            raise ValueError("command_timeout_s must be > 0")
        if self.command_sandbox not in {"none", "bwrap", "auto"}:
            raise ValueError("command_sandbox must be one of: none, bwrap, auto")
        if self.write_root is not None and not self.write_root.is_absolute():
            raise ValueError("write_root must be an absolute path")
        for prefix in self.command_allowlist:
            if not prefix or any(not part.strip() for part in prefix):
                raise ValueError("command_allowlist entries must be non-empty argv prefixes")
        seen: set[str] = set()
        for name, allowlist in self.command_profiles:
            if not name.strip() or name == "default":
                raise ValueError("command profile names must be non-empty and not 'default'")
            if name in seen:
                raise ValueError(f"duplicate command profile: {name}")
            seen.add(name)
            for prefix in allowlist:
                if not prefix or any(not part.strip() for part in prefix):
                    raise ValueError(f"command profile {name} has an empty argv prefix")


@dataclass(frozen=True, slots=True)
class KnowledgeSettings:
    """知识库的部署参数（ADR-016/017）。

    前置条件：db_path 与 docs_root 为绝对路径；chunk_chars >= 100；top_k >= 1。
    后置条件：不可变；作为 KnowledgeBase 的构造参数注入。
    禁止事项：不要在运行期改 db_path（会导致索引与检索指向不同库）。
    """

    db_path: Path
    docs_root: Path
    embed_alias: str
    chunk_chars: int
    top_k: int
    candidate_k: int = 20
    parent_chars: int = 2400
    hybrid: bool = True
    rrf_k: int = 60
    # 相对 docs_root 的排除模式；WORKLOG 是记录，不是可检索知识。
    exclude_globs: tuple[str, ...] = ("WORKLOG.md", "Agents_opinion/*")

    def __post_init__(self) -> None:
        if self.chunk_chars < 100:
            raise ValueError("chunk_chars must be >= 100")
        if self.top_k < 1:
            raise ValueError("top_k must be >= 1")
        if self.candidate_k < self.top_k:
            raise ValueError("candidate_k must be >= top_k")
        if self.parent_chars < 100:
            raise ValueError("parent_chars must be >= 100")
        if self.rrf_k < 1:
            raise ValueError("rrf_k must be >= 1")
        if any(not pattern.strip() for pattern in self.exclude_globs):
            raise ValueError("exclude_globs must not contain empty patterns")
        if not self.embed_alias.strip():
            raise ValueError("embed_alias must be non-empty")


@dataclass(frozen=True, slots=True)
class MemorySettings:
    """长期记忆与请愿队列的部署参数（ADR-018）。

    前置条件：path 与 requests_path 为绝对路径；recall_limit >= 0。
    后置条件：不可变；作为 JsonlMemoryStore 的构造参数注入。
    禁止事项：不要把 path 指向 ledger 目录之外的临时文件（审计需要可追溯）。
    """

    path: Path
    requests_path: Path
    recall_limit: int

    def __post_init__(self) -> None:
        if self.recall_limit < 0:
            raise ValueError("recall_limit must be >= 0")


@dataclass(frozen=True, slots=True)
class MediaSettings:
    """媒体感知的部署参数（ADR-020）。

    前置条件：asr_alias / vl_alias 可为空；非空时必须在 [models] 中登记；两个上限 > 0。
    后置条件：不可变；作为 LlamaCppMedia 的构造参数注入。
    禁止事项：不要把上限调到超过内存可承受的 base64 体积（约 1.34 倍）。
    """

    asr_alias: str
    vl_alias: str
    max_audio_bytes: int
    max_image_bytes: int

    def __post_init__(self) -> None:
        # 媒体模型是可选能力：空 alias 表示该模态未启用（ADR-042）。
        if not isinstance(self.asr_alias, str) or not isinstance(self.vl_alias, str):
            raise ValueError("media aliases must be strings")
        if self.max_audio_bytes <= 0 or self.max_image_bytes <= 0:
            raise ValueError("media size limits must be positive")


@dataclass(frozen=True, slots=True)
class WebSettings:
    """Web UI 的监听地址（ADR-027）。

    前置条件：host 非空；port ∈ [1, 65535]。
    后置条件：不可变；默认只绑定回环地址。
    禁止事项：不要在未加认证时默认绑定 0.0.0.0。
    """

    host: str = "127.0.0.1"
    port: int = 8765
    # 从该环境变量读取 Bearer token；空字符串表示不启用认证。
    auth_token_env: str = "AGENT_WEB_TOKEN"
    tls_cert: Path | None = None
    tls_key: Path | None = None

    def __post_init__(self) -> None:
        if not self.host.strip():
            raise ValueError("web host must be non-empty")
        if not 1 <= self.port <= 65535:
            raise ValueError("web port must be in [1, 65535]")
        if self.port in _RESERVED_PORTS:
            raise ValueError(f"web port {self.port} is reserved/occupied; choose another")
        if (self.tls_cert is None) != (self.tls_key is None):
            raise ValueError("web tls_cert and tls_key must be configured together")
        for path in (self.tls_cert, self.tls_key):
            if path is not None and not path.is_absolute():
                raise ValueError("web tls paths must be absolute")


@dataclass(frozen=True, slots=True)
class AppConfig:
    """一次进程启动所需的全部配置。

    前置条件：由 load 构造并通过校验。
    后置条件：不可变；所有路径均为绝对路径。
    禁止事项：不要在运行期修改目录或模型列表。
    """

    policy: HarnessPolicy
    models: Mapping[str, ModelProfile]
    main_alias: str
    review_alias: str
    llama_binary: Path
    ledger_dir: Path
    log_dir: Path
    lock_path: Path
    brain_timeout_s: float
    health_timeout_s: float
    review_max_tokens: int
    workspace_root: Path
    workspace_limits: WorkspaceLimits
    guardrail: GuardrailPolicy
    knowledge: KnowledgeSettings
    memory: MemorySettings
    media: MediaSettings
    web: WebSettings = field(default_factory=WebSettings)

    def model(self, alias: str) -> ModelProfile:
        """按 alias 取模型档案。

        前置条件：alias 非空。
        后置条件：返回对应 ModelProfile。
        禁止事项：不要为未知 alias 编造档案。
        异常：ConfigError（alias 不存在）。
        """
        try:
            return self.models[alias]
        except KeyError as exc:
            raise ConfigError(f"unknown model alias: {alias}") from exc


def load(
    path: Path | None = None,
    *,
    model_overrides: Mapping[str, str] | None = None,
    disabled_aliases: Iterable[str] = (),
) -> AppConfig:
    """读取并校验配置。

    前置条件：path 为 None 时使用包内 agent/config/defaults.toml；相对路径按进程 cwd 解析。
    后置条件：返回绝对路径化的 AppConfig；不创建任何目录。
    禁止事项：不要在此启动进程或探测端口（保持纯函数语义）。
    异常：ConfigError（文件缺失、TOML 非法、键缺失、路径不存在、端口冲突）。
    """
    config_path = Path(path) if path is not None else _DEFAULT_CONFIG
    try:
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {config_path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {config_path}: {exc}") from exc

    base = Path.cwd()
    harness_table = _table(raw, "harness")
    policy = HarnessPolicy(
        max_steps=int(_value(harness_table, "max_steps")),
        # ADR-037：评审打回与动态步数；旧配置缺省时使用标准值。
        max_review_repairs=int(harness_table.get("max_review_repairs", 2)),
        repair_step_bonus=int(harness_table.get("repair_step_bonus", 4)),
        temperature=float(_value(harness_table, "temperature")),
        max_tokens=int(_value(harness_table, "max_tokens")),
        # 新增预算键对旧配置保持向后兼容（缺省即 ADR-021 的标准值）。
        max_attempts=int(harness_table.get("max_attempts", 3)),
        escalation_rounds=int(harness_table.get("escalation_rounds", 3)),
    )
    main_alias = str(_value(harness_table, "main_alias"))
    review_alias = str(_value(harness_table, "review_alias"))

    paths_table = _table(raw, "paths")
    llama_binary = _resolve(base, str(_value(paths_table, "llama_binary")))
    ledger_dir = _resolve(base, str(_value(paths_table, "ledger_dir")))
    log_dir = _resolve(base, str(_value(paths_table, "log_dir")))
    lock_path = _resolve(base, str(_value(paths_table, "lock_file")))

    models_table = _table(raw, "models")
    models: dict[str, ModelProfile] = {}
    for alias, entry in models_table.items():
        models[alias] = _model_profile(alias, _as_table(entry, f"models.{alias}"), base)

    for alias, value in (model_overrides or {}).items():
        if alias not in models:
            raise ConfigError(f"--model alias not defined under [models]: {alias}")
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"--model {alias} path must be non-empty")
        models[alias] = replace(models[alias], gguf=_resolve(base, value))

    workspace_table = _table(raw, "workspace")
    workspace_root = _resolve(base, str(_value(workspace_table, "root")))
    raw_write_root = workspace_table.get("write_root", "artifacts")
    write_root = None if raw_write_root in (None, "") else _resolve(base, str(raw_write_root))
    try:
        workspace_limits = WorkspaceLimits(
            max_read_bytes=int(_value(workspace_table, "max_read_bytes")),
            max_list_entries=int(_value(workspace_table, "max_list_entries")),
            write_root=write_root,
            max_write_bytes=int(workspace_table.get("max_write_bytes", 65536)),
            command_allowlist=_parse_allowlist(workspace_table.get("command_allowlist", [])),
            command_timeout_s=float(workspace_table.get("command_timeout_s", 15.0)),
            max_command_output_bytes=int(workspace_table.get("max_command_output_bytes", 65536)),
            command_sandbox=str(workspace_table.get("command_sandbox", "auto")),
            command_profiles=_parse_command_profiles(workspace_table.get("command_profiles", [])),
        )
    except ValueError as exc:
        raise ConfigError(f"invalid [workspace]: {exc}") from exc
    if write_root is not None and not write_root.is_relative_to(workspace_root):
        raise ConfigError("workspace.write_root must be inside workspace.root")

    guardrail_table = _table(raw, "guardrail")
    deny_names = tuple(str(name) for name in _value(guardrail_table, "deny_names"))
    guardrail = GuardrailPolicy(root=workspace_root, deny_names=deny_names, write_root=write_root)

    knowledge_table = _table(raw, "knowledge")
    knowledge = KnowledgeSettings(
        db_path=_resolve(base, str(_value(knowledge_table, "db_path"))),
        docs_root=_resolve(base, str(_value(knowledge_table, "docs_root"))),
        embed_alias=str(_value(knowledge_table, "embed_alias")),
        chunk_chars=int(_value(knowledge_table, "chunk_chars")),
        top_k=int(_value(knowledge_table, "top_k")),
        candidate_k=int(knowledge_table.get("candidate_k", 20)),
        parent_chars=int(knowledge_table.get("parent_chars", 2400)),
        hybrid=bool(knowledge_table.get("hybrid", True)),
        rrf_k=int(knowledge_table.get("rrf_k", 60)),
        exclude_globs=tuple(
            str(pattern)
            for pattern in knowledge_table.get("exclude", ["WORKLOG.md", "Agents_opinion/*"])
        ),
    )

    memory_table = _table(raw, "memory")
    memory = MemorySettings(
        path=_resolve(base, str(_value(memory_table, "path"))),
        requests_path=_resolve(base, str(_value(memory_table, "requests_path"))),
        recall_limit=int(_value(memory_table, "recall_limit")),
    )

    media_table = _table(raw, "media")
    media = MediaSettings(
        asr_alias=str(_value(media_table, "asr_alias")),
        vl_alias=str(_value(media_table, "vl_alias")),
        max_audio_bytes=int(_value(media_table, "max_audio_bytes")),
        max_image_bytes=int(_value(media_table, "max_image_bytes")),
    )

    disabled = {str(alias).strip() for alias in disabled_aliases if str(alias).strip()}
    for alias in sorted(disabled):
        if alias in {main_alias, knowledge.embed_alias}:
            raise ConfigError(f"required model cannot be disabled: {alias}")
        if alias not in models:
            # 配置里本来就没有这个可选模型：视作已经禁用。
            continue
        models.pop(alias)
        if review_alias == alias:
            review_alias = ""
        if media.asr_alias == alias:
            media = replace(media, asr_alias="")
        if media.vl_alias == alias:
            media = replace(media, vl_alias="")

    # 可选模型未出现在 [models] 时自动视为未启用（便于他人复用配置）。
    if review_alias and review_alias not in models:
        review_alias = ""
    if media.asr_alias and media.asr_alias not in models:
        media = replace(media, asr_alias="")
    if media.vl_alias and media.vl_alias not in models:
        media = replace(media, vl_alias="")

    web_raw = raw.get("web", {})
    if web_raw is None:
        web_raw = {}
    if not isinstance(web_raw, Mapping):
        raise ConfigError("web must be a table")
    raw_cert = web_raw.get("tls_cert", "")
    raw_key = web_raw.get("tls_key", "")
    tls_cert = None if raw_cert in (None, "") else _resolve(base, str(raw_cert))
    tls_key = None if raw_key in (None, "") else _resolve(base, str(raw_key))
    for path in (tls_cert, tls_key):
        if path is not None and not path.is_file():
            raise ConfigError(f"web TLS file not found: {path}")
    try:
        web = WebSettings(
            host=str(web_raw.get("host", "127.0.0.1")),
            port=int(web_raw.get("port", 8765)),
            auth_token_env=str(web_raw.get("auth_token_env", "AGENT_WEB_TOKEN")),
            tls_cert=tls_cert,
            tls_key=tls_key,
        )
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"invalid [web]: {exc}") from exc

    _validate(
        models=models,
        main_alias=main_alias,
        review_alias=review_alias,
        binary=llama_binary,
        workspace_root=workspace_root,
        embed_alias=knowledge.embed_alias,
        media_aliases=(media.asr_alias, media.vl_alias),
    )
    return AppConfig(
        policy=policy,
        models=models,
        main_alias=main_alias,
        review_alias=review_alias,
        llama_binary=llama_binary,
        ledger_dir=ledger_dir,
        log_dir=log_dir,
        lock_path=lock_path,
        brain_timeout_s=float(_value(harness_table, "brain_timeout_s")),
        health_timeout_s=float(_value(harness_table, "health_timeout_s")),
        review_max_tokens=int(_value(harness_table, "review_max_tokens")),
        workspace_root=workspace_root,
        workspace_limits=workspace_limits,
        guardrail=guardrail,
        knowledge=knowledge,
        memory=memory,
        media=media,
        web=web,
    )


def _model_profile(alias: str, table: Mapping[str, Any], base: Path) -> ModelProfile:
    """把一个 [models.<alias>] 表转成 ModelProfile。

    前置条件：table 来自 tomllib。
    后置条件：返回经过基础校验的 ModelProfile。
    禁止事项：不要为缺失键填默认值（默认值会掩盖配置漂移）。
    异常：ConfigError。
    """
    gguf = _resolve(base, str(_value(table, "gguf")))
    raw_mmproj = table.get("mmproj")
    mmproj = None if raw_mmproj in (None, "") else _resolve(base, str(raw_mmproj))
    layers_raw = int(table.get("n_gpu_layers", -1))
    return ModelProfile(
        alias=alias,
        gguf=gguf,
        host=str(table.get("host", "127.0.0.1")),
        port=int(_value(table, "port")),
        n_ctx=int(_value(table, "n_ctx")),
        n_gpu_layers=None if layers_raw < 0 else layers_raw,
        mmproj=mmproj,
        embeddings=bool(table.get("embeddings", False)),
    )


def _validate(
    *,
    models: Mapping[str, ModelProfile],
    main_alias: str,
    review_alias: str,
    embed_alias: str,
    media_aliases: tuple[str, str],
    binary: Path,
    workspace_root: Path,
) -> None:
    """校验跨字段约束。

    前置条件：单个 profile 已完成字段级解析。
    后置条件：全部合法则返回；否则抛 ConfigError。
    禁止事项：不要在此做"自动修正"（例如把缺失的模型换成别的）。
    异常：ConfigError（无模型、main/embed 缺失、启用的可选模型缺 mmproj、路径不存在、端口重复、ctx 过小、workspace 根不存在）。
    """
    if not models:
        raise ConfigError("at least one model profile is required")
    if main_alias not in models:
        raise ConfigError(f"harness.main_alias={main_alias!r} is not defined under [models]")
    if review_alias and review_alias not in models:
        raise ConfigError(f"harness.review_alias={review_alias!r} is not defined under [models]")
    if embed_alias not in models:
        raise ConfigError(f"knowledge.embed_alias={embed_alias!r} is not defined under [models]")
    if not models[embed_alias].embeddings:
        raise ConfigError(f"models.{embed_alias} must set embeddings = true")
    for media_alias in media_aliases:
        if not media_alias:
            continue
        if media_alias not in models:
            raise ConfigError(f"[media] alias {media_alias!r} is not defined under [models]")
        if models[media_alias].mmproj is None:
            # 配置层先挡住：没有 mmproj 的媒体模型只能读文本（ADR-020）。
            raise ConfigError(f"models.{media_alias} must set mmproj for media input")
    if not binary.is_file():
        raise ConfigError(f"llama_binary not found: {binary}")
    if not workspace_root.is_dir():
        raise ConfigError(f"workspace.root is not a directory: {workspace_root}")

    active_aliases = {main_alias, embed_alias}
    if review_alias:
        active_aliases.add(review_alias)
    active_aliases.update(alias for alias in media_aliases if alias)

    ports: dict[int, str] = {}
    for profile in models.values():
        if profile.alias not in active_aliases:
            # 未启用的可选模型不要求本地存在；--list-models 仍会显示它。
            continue
        if not profile.gguf.is_file():
            raise ConfigError(f"models.{profile.alias}.gguf not found: {profile.gguf}")
        if profile.mmproj is not None and not profile.mmproj.is_file():
            raise ConfigError(f"models.{profile.alias}.mmproj not found: {profile.mmproj}")
        if not 1 <= profile.port <= 65535:
            raise ConfigError(f"models.{profile.alias}.port out of range: {profile.port}")
        if profile.port in _RESERVED_PORTS:
            raise ConfigError(
                f"models.{profile.alias}.port {profile.port} is reserved/occupied; choose another"
            )
        if profile.n_ctx < 512:
            raise ConfigError(f"models.{profile.alias}.n_ctx must be >= 512")
        if profile.port in ports:
            raise ConfigError(
                f"port {profile.port} used by both {ports[profile.port]} and {profile.alias}"
            )
        ports[profile.port] = profile.alias


def _parse_command_profiles(
    value: Any,
) -> tuple[tuple[str, tuple[tuple[str, ...], ...]], ...]:
    """把 [[workspace.command_profiles]] 解析成 (name, allowlist) 元组。

    前置条件：value 来自 TOML，缺省为 []。
    后置条件：返回不可变的 profile 列表；default 由 command_allowlist 隐式提供。
    禁止事项：不要接受重复 name 或名为 default 的额外 profile。
    异常：ConfigError（不是数组、缺 name、allowlist 非法）。
    """
    if not isinstance(value, list):
        raise ConfigError("workspace.command_profiles must be an array of tables")
    profiles: list[tuple[str, tuple[tuple[str, ...], ...]]] = []
    seen: set[str] = set()
    for index, entry in enumerate(value):
        if not isinstance(entry, Mapping):
            raise ConfigError(f"workspace.command_profiles[{index}] must be a table")
        name = str(entry.get("name", "")).strip()
        if not name:
            raise ConfigError(f"workspace.command_profiles[{index}].name must be non-empty")
        if name == "default" or name in seen:
            raise ConfigError(f"workspace.command_profiles[{index}].name is reserved/duplicate: {name}")
        seen.add(name)
        profiles.append((name, _parse_allowlist(entry.get("allowlist", []))))
    return tuple(profiles)


def _parse_allowlist(value: Any) -> tuple[tuple[str, ...], ...]:
    """把 TOML 的嵌套数组解析成 argv 前缀元组。

    前置条件：value 来自 workspace.command_allowlist（缺省为 []）。
    后置条件：返回不可变的前缀元组；空列表表示禁用命令执行。
    禁止事项：不要接受字符串命令并自行 shlex 拆分（会引入 shell 语义）。
    异常：ConfigError（不是数组、空元素、非字符串部分）。
    """
    if not isinstance(value, list):
        raise ConfigError("workspace.command_allowlist must be an array of arrays")
    prefixes: list[tuple[str, ...]] = []
    for index, entry in enumerate(value):
        if not isinstance(entry, list) or not entry:
            raise ConfigError(f"workspace.command_allowlist[{index}] must be a non-empty array")
        parts: list[str] = []
        for part in entry:
            if not isinstance(part, str) or not part.strip():
                raise ConfigError(f"workspace.command_allowlist[{index}] parts must be non-empty strings")
            parts.append(part)
        prefixes.append(tuple(parts))
    return tuple(prefixes)


def _resolve(base: Path, value: str) -> Path:
    """把配置中的路径解析为绝对路径。

    前置条件：value 非空。
    后置条件：返回 (base / value).expanduser().resolve()。
    禁止事项：不要解析相对路径到包目录（配置属于部署，不属于代码）。
    异常：ConfigError（空值）。
    """
    if not value.strip():
        raise ConfigError("empty path in config")
    path = Path(value).expanduser()
    return path if path.is_absolute() else (base / path).resolve()


def _table(raw: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    """取一个 TOML 子表。

    前置条件：raw 为解析后的顶层表。
    后置条件：返回子表 Mapping。
    禁止事项：不要返回 None。
    异常：ConfigError（键缺失或类型不对）。
    """
    return _as_table(raw.get(key), key)


def _as_table(value: Any, key: str) -> Mapping[str, Any]:
    """断言 value 是 TOML 表。

    前置条件：无。
    后置条件：返回 Mapping。
    禁止事项：不要把标量当表使用。
    异常：ConfigError。
    """
    if not isinstance(value, Mapping):
        raise ConfigError(f"config section [{key}] is missing or not a table")
    return value


def _value(table: Mapping[str, Any], key: str) -> Any:
    """取必需标量。

    前置条件：table 为 Mapping。
    后置条件：返回值非 None。
    禁止事项：不要为缺失键提供默认值。
    异常：ConfigError（缺失）。
    """
    if key not in table:
        raise ConfigError(f"missing required config key: {key}")
    return table[key]
