"""ports — 核心依赖的抽象端口（brain / tool / ui / embedder / vector store）。

Module: agent.ports
Responsibility: 定义核心与实现之间的接口与传值对象，使 core 不依赖任何适配器。
Depends on: stdlib (dataclasses, typing) + agent.core.protocol
Depended by: core.harness, adapters.llama_cpp, adapters.embeddings, adapters.sqlite_vectors,
    adapters.handle_to_master, knowledge.*, tools.*, ui.console, app
ADRs: ADR-004（BrainPort 只暴露 complete/health）, ADR-005（Tool 失败以 ToolResult 表达）,
    ADR-014（BrainRequest.role）, ADR-015（BrainRequest.disable_thinking）,
    ADR-016（Chunk/VectorStore）, ADR-017（EmbedderPort）, ADR-021（MasterPort / ask_consent）,
    ADR-023（RolePort）, ADR-025（InteractiveUiPort）, ADR-027（Web UI）, ADR-034（RuntimeInfoPort）
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from agent.core.protocol import Envelope, ErrorCode, ToolCall, ToolResult, ToolSpec


@dataclass(frozen=True, slots=True)
class BrainRequest:
    """一次模型调用的输入。

    前置条件：messages 至少一条；temperature ∈ [0, 2]；max_tokens > 0。
    后置条件：json_schema 为 None 表示自由文本，非 None 时要求模型输出该 schema。
    禁止事项：禁止在 messages 中放不可 JSON 序列化的对象（适配器会直接 dumps）。
    """

    messages: tuple[Mapping[str, Any], ...]
    json_schema: Mapping[str, Any] | None = None
    temperature: float = 0.2
    max_tokens: int = 512
    # 用哪个模型由角色表达（ADR-014）：core 不认识 alias，只有 router 认识。
    role: str = "main"
    # 结构化短输出可关闭推理通道（ADR-015）；模板不支持时该标记被忽略。
    disable_thinking: bool = False


@dataclass(frozen=True, slots=True)
class BrainReply:
    """一次模型调用的输出。

    前置条件：content 必须是 dict（受 schema 约束时为解析结果）。
    后置条件：elapsed_s 为 FP64 秒；tokens 来自服务端 usage。
    禁止事项：禁止把 raw 文本当结构化结果使用。
    """

    content: Mapping[str, Any]
    raw: str
    tokens: int
    elapsed_s: float


class BrainError(RuntimeError):
    """BrainPort 实现层面的失败（网络、HTTP、解析、缺少字段）。

    前置条件：无。
    后置条件：异常信息含可定位的上下文（URL、状态码或解析错误），但不含密钥。
    禁止事项：不要用它表达"模型答得不好"（那是 Review 的职责）；不要吞掉后返回空回复。
    异常来源：adapters.llama_cpp、未来的 HandleToMaster。
    """


class RoleError(BrainError):
    """请求了未登记的角色（ADR-014/ADR-023）。

    前置条件：role 来自 BrainRequest.role 或 delegate.switch_role 的参数。
    后置条件：异常信息列出已知角色，便于模型/用户修正。
    禁止事项：不要回落到 main（会让“切换失败”悄悄变成“没切换”）。
    异常来源：adapters.llama_cpp.RoleRegistry。
    """


class EscalationError(RuntimeError):
    """升级端口失败：未配置、网络错误、远端拒绝或返回空答案（ADR-021）。

    前置条件：无。
    后置条件：异常信息可定位原因，但**不得包含 API key 或未脱敏原文**。
    禁止事项：不要在未配置时伪造远端回答；不要自动回落到任何默认地址。
    异常来源：adapters.handle_to_master、core.escalation。
    """


class EmbeddingError(RuntimeError):
    """EmbedderPort 实现层面的失败（服务不可用、维度不一致、响应格式错误）。

    前置条件：无。
    后置条件：异常信息含可定位的上下文（状态码/维度），但不含密钥或全文内容。
    禁止事项：不要用它表达"检索结果不相关"（那是 score 的职责）。
    异常来源：adapters.embeddings。
    """


class MediaError(RuntimeError):
    """MediaPort 实现层面的失败（缺少 mmproj、文件过大、上游拒绝）。

    前置条件：code 为该错误应传播成的协议错误码（见 ErrorCode）。
    后置条件：code 与 message 可直接映射成 ErrorInfo，工具层不再猜测。
    禁止事项：不要在缺少 mmproj 时退回"纯文本模型"继续跑（会静默丢失媒体内容）。
    异常来源：adapters.media。
    """

    def __init__(self, code: ErrorCode, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class MediaResult:
    """一次媒体感知的结果（ADR-020）。

    前置条件：text 已去除首尾空白；elapsed_s 为 FP64 秒。
    后置条件：text 可直接作为工具结果交给主脑。
    禁止事项：不要在此做摘要（摘要属于模型，不是适配器）。
    """

    text: str
    model_alias: str
    elapsed_s: float


@runtime_checkable
class BrainPort(Protocol):
    """模型端口的唯一接口（ADR-004）。

    契约：
    - complete：给定 BrainRequest 返回 BrainReply；失败抛 BrainError。
    - health：服务可用返回 True，不可用返回 False，不抛异常。
    """

    def complete(self, request: BrainRequest) -> BrainReply:
        """执行一次推理。

        前置条件：底层服务已就绪（由调用方先 ensure 模型）。
        后置条件：返回 BrainReply；json_schema 非空时 content 必为 dict。
        禁止事项：不得在内部重试（重试策略属于 harness 的预算层）。
        异常：BrainError（网络、HTTP 非 2xx、JSON 解析失败、缺少 choices）。
        """
        ...

    def health(self) -> bool:
        """探测服务健康。

        前置条件：无。
        后置条件：返回布尔值，绝不抛异常。
        禁止事项：不要用它判断模型是否"答得好"。
        异常：无（内部吞掉并返回 False）。
        """
        ...


@runtime_checkable
class RuntimeInfoPort(Protocol):
    """模型运行时事实的只读接口（ADR-034）。

    契约：begin_turn 只重置统计窗口；snapshot 不得触发模型加载、启停或副作用。
    """

    def begin_turn(self) -> None:
        """标记新一轮开始，清空“本轮使用过的模型”集合。

        前置条件：调用方保证同一时刻只有一轮在执行。
        后置条件：之后的 snapshot 只包含本轮 ensure 过的模型。
        禁止事项：不要在 turn 进行中调用（会丢失已记录的使用情况）。
        异常：无。
        """
        ...

    def snapshot(self) -> Mapping[str, int | None]:
        """返回本轮 ensure 过的 alias → 实测 ngl。

        前置条件：Harness 已在本轮开始时调用 begin_turn()。
        后置条件：只读取本轮统计；None 表示该模型使用 `--fit on`。
        禁止事项：不要为本轮未用的模型编造 ngl，也不要在此 ensure。
        异常：无。
        """
        ...


@runtime_checkable
class RolePort(Protocol):
    """当前角色与 role→alias 映射的只读/切换接口（ADR-014/ADR-023）。

    契约：resolve 不改变状态；switch_role 只改“下一次主脑调用用谁”，不启动模型。
    """

    @property
    def current(self) -> str:
        """当前主脑角色名（默认 main）。

        前置条件：无。
        后置条件：返回值必定是 resolve 可识别的角色。
        禁止事项：不要在这里触发模型加载或 Turn 状态变化。
        异常：无。
        """
        ...

    def resolve(self, role: str) -> str:
        """把角色解析成模型 alias。

        前置条件：role 为已登记角色。
        后置条件：返回对应 alias；未知角色抛 RoleError。
        禁止事项：不要为未知角色回落 main。
        异常：RoleError。
        """
        ...

    def switch_role(self, role: str) -> str:
        """切换当前角色并返回其 alias（不加载模型）。

        前置条件：role 为已登记角色。
        后置条件：current == role；返回角色对应 alias。
        禁止事项：不要新开 Turn 或清空上下文（ledger/记忆保持原样）。
        异常：RoleError。
        """
        ...


@runtime_checkable
class MasterPort(Protocol):
    """场外主脑端口的唯一接口（ADR-021）。

    契约：payload 必须是**已脱敏**的 JSON 对象；失败抛 EscalationError，不返回空串。
    """

    def complete(self, payload: Mapping[str, Any]) -> str:
        """把脱敏后的升级请求发给场外模型并返回答案文本。

        前置条件：payload 已由 Redactor 处理；实现不得再修改或补写原文。
        后置条件：返回非空字符串；不进行内部重试（重试会放大费用与外发面）。
        禁止事项：不要内置默认端点或默认密钥；不要在异常信息里回显 key。
        异常：EscalationError（未配置、网络/HTTP 失败、响应为空）。
        """
        ...


@runtime_checkable
class Tool(Protocol):
    """工具的唯一接口（ADR-005）。

    契约：
    - spec：静态描述，供提示词与 guardrail 使用。
    - invoke：永不抛异常表达业务失败，失败必须返回 ok=False 的 ToolResult。
    """

    @property
    def spec(self) -> ToolSpec:
        """返回工具自描述。

        前置条件：实现方在构造时已完成校验。
        后置条件：同一实例多次调用返回等价对象。
        禁止事项：不要返回可变引用后又在外部修改。
        异常：无。
        """
        ...

    def invoke(self, call: ToolCall) -> ToolResult:
        """执行工具。

        前置条件：call.tool 等于 self.spec.name。
        后置条件：返回 ToolResult；call_id 与入参一致。
        禁止事项：不要吞掉 KeyboardInterrupt/SystemExit；不要返回裸 dict。
        异常：仅允许不可恢复的编程错误（如 MemoryError）逃逸。
        """
        ...


@runtime_checkable
class UiPort(Protocol):
    """界面端口的唯一接口；core 只通过它输出监督流与结论。

    契约：render 每条信封调用一次；report 在 turn 结束时调用一次。
    """

    def render(self, envelope: Envelope) -> None:
        """渲染一条事件。

        前置条件：envelope 已通过协议校验。
        后置条件：输出发生在调用内，不缓存在 UI 里跨 turn 复用。
        禁止事项：不要修改 envelope，也不要抛异常打断 harness。
        异常：无（实现方应自行吞掉 BrokenPipe 等输出错误）。
        """
        ...

    def report(self, envelope: Envelope, ledger_path: Path) -> None:
        """输出最终结论与审计路径。

        前置条件：envelope.kind 为 ANSWER 或 ERROR。
        后置条件：人类可读的结论已输出；ledger_path 可用于复查。
        禁止事项：不要在 report 中再次逐条渲染事件。
        异常：无。
        """
        ...

    def ask_consent(self, question: str) -> bool:
        """就一次外发升级向用户征求同意（ADR-021）。

        前置条件：question 含失败摘要与**已脱敏**的 payload 预览。
        后置条件：同意返回 True；拒绝/非交互环境返回 False（默认拒绝）。
        禁止事项：不要在非 TTY 环境自行假设同意；不要把同意结果写回 question。
        异常：无（实现方应把输入错误按拒绝处理）。
        """
        ...


@runtime_checkable
class InteractiveUiPort(UiPort, Protocol):
    """支持多轮输入与屏幕生命周期的 UI 端口（TUI 用）。

    契约：read_user_input 在空闲时阻塞读一行；返回 None 表示退出。
    """

    def read_user_input(self) -> str | None:
        """读取一行用户输入。

        前置条件：UI 已初始化且当前处于空闲状态。
        后置条件：返回去除换行的文本；用户要求退出/输入结束返回 None。
        禁止事项：不要在 turn 执行期间调用（Harness 是同步的）。
        异常：无（输入错误按 None 处理）。
        """
        ...

    def show_error(self, message: str) -> None:
        """显示一条非致命运行错误（不结束会话）。

        前置条件：message 非空。
        后置条件：错误已显示；UI 仍可继续读入下一条用户输入。
        禁止事项：不要用它替代 harness 的 ERROR 信封（turn 内错误仍走 ledger）。
        异常：无。
        """
        ...

    def close(self) -> None:
        """释放终端状态（可重复调用）。

        前置条件：无。
        后置条件：终端已恢复为正常模式；之后不再渲染。
        禁止事项：不要在 close 后继续调用 render。
        异常：无。
        """
        ...


@dataclass(frozen=True, slots=True)
class Chunk:
    """知识库中的最小检索单元（ADR-016）。

    前置条件：chunk_id 在库内唯一；ordinal 为文档内顺序。
    后置条件：text 含标题前缀（便于模型判断上下文）。
    禁止事项：不要把未清洗的二进制内容放进来。
    """

    chunk_id: str
    doc_id: str
    heading: str
    ordinal: int
    text: str
    # 父文档/父节 id；当前按文档粒度扩展（ADR-038）。
    parent_id: str = ""


@runtime_checkable
class EmbedderPort(Protocol):
    """文本向量化端口（ADR-017）。

    契约：embed 对同一文本返回同一向量（由服务端保证），维度在一批内恒定。
    """

    def embed(self, texts: Sequence[str]) -> list[Sequence[float]]:
        """把一批文本转成向量。

        前置条件：texts 非空且每条非空。
        后置条件：返回与 texts 等长的向量列表，每个向量为 FP64 序列。
        禁止事项：不要在这里做截断或改写文本（索引与查询必须用同一套文本）。
        异常：EmbeddingError（服务不可用、维度不一致、响应格式错误）。
        """
        ...


@runtime_checkable
class VectorStore(Protocol):
    """向量存储端口（ADR-016）。

    契约：replace_all 是幂等的全量替换；load_all 返回全部记录与向量。
    """

    def replace_all(self, chunks: Sequence[Chunk], vectors: Sequence[Sequence[float]]) -> None:
        """全量替换索引内容。

        前置条件：chunks 与 vectors 等长；每条向量维度一致。
        后置条件：旧索引被清空，新内容可被 load_all 读到。
        禁止事项：不要做增量合并（语义是重置，见 ADR-016）。
        异常：OSError/ValueError（存储不可写、维度不一致）。
        """
        ...

    def load_all(self) -> list[tuple[Chunk, Sequence[float]]]:
        """读取全部索引内容。

        前置条件：无（空库返回空列表）。
        后置条件：返回 (chunk, vector) 列表；向量为 FP64。
        禁止事项：不要在此排序或过滤（排序属于检索策略）。
        异常：OSError（存储不可读）。
        """
        ...

    def count(self) -> int:
        """返回已索引的 chunk 数。

        前置条件：无。
        后置条件：空库返回 0。
        禁止事项：不要为了统计而加载全部向量。
        异常：OSError。
        """
        ...

    def dimension(self) -> int | None:
        """返回已索引向量的维度。

        前置条件：无。
        后置条件：空库返回 None。
        禁止事项：不要假设维度恒为 1024（换模型会变）。
        异常：OSError。
        """
        ...


@runtime_checkable
class MemoryPort(Protocol):
    """长期记忆端口（ADR-018）。

    契约：append 只追加；recent 返回按时间升序的最近条目。
    """

    def append(self, entry: Mapping[str, Any]) -> None:
        """追加一条反思记录。

        前置条件：entry 可 JSON 序列化且含 turn_id。
        后置条件：文件行数 +1；历史记录不被修改。
        禁止事项：不要重写或压缩历史文件（审计前提）。
        异常：OSError。
        """
        ...

    def recent(self, limit: int) -> list[Mapping[str, Any]]:
        """读取最近 limit 条记录。

        前置条件：limit >= 0。
        后置条件：返回按时间升序的列表；文件不存在时返回空列表。
        禁止事项：不要在此做语义筛选（注入策略属于 core）。
        异常：OSError。
        """
        ...

    def record_tool_request(self, payload: Mapping[str, Any]) -> None:
        """把"想改工具"的请求写进队列（不自动生效）。

        前置条件：payload 含 tool 与 reason。
        后置条件：请求进入 tool_requests 队列文件。
        禁止事项：不要直接修改工具注册表或提示词。
        异常：OSError。
        """
        ...


@runtime_checkable
class MediaPort(Protocol):
    """媒体感知端口（ADR-020）。

    契约：失败必须以 MediaError 抛出（携带协议错误码），不得返回空字符串冒充成功。
    """

    def transcribe(self, path: Path) -> MediaResult:
        """把一段音频转成文本。

        前置条件：path 已由护栏归一化到工作区内；文件存在且在上限内。
        后置条件：返回 MediaResult；text 为转写结果（可能为空串，表示无人声）。
        禁止事项：不要在缺 mmproj 时用文本模型"猜"内容。
        异常：MediaError（MISSING_MMPROJ / BAD_ARGS / UPSTREAM_ERROR）、OSError。
        """
        ...

    def describe_image(self, path: Path, prompt: str) -> MediaResult:
        """描述一张图片。

        前置条件：path 已归一化；prompt 非空（默认由工具提供）。
        后置条件：返回 MediaResult；text 为模型的描述。
        禁止事项：不要缓存上一次的结果（图片可能被覆盖）。
        异常：MediaError、OSError。
        """
        ...
