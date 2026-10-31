# ADR-004：BrainPort 抽象与 llama-server 的 JSON 约束调用

- **状态**：已采纳（2026-10-29）
- **关联代码**：`agent/ports.py:BrainPort`、`agent/adapters/llama_cpp.py:LlamaCppBrain.complete`、`LlamaCppBrain.health`、`agent/core/harness.py:Harness._plan`、`agent/core/harness.py:Harness._answer`
- **关联 ADR**：ADR-002（谁负责拉起进程）、ADR-003（HTTP 客户端选型）、ADR-006（TB-0 只接主脑）、`docs/pre_architecture.md` ADR-8
- **上游文档**：`docs/pre_architecture.md` §5.3、§7

## 背景

主脑与次脑是不同模型、不同上下文长度、不同角色，但对调用方只应暴露"给消息与 schema，拿回 JSON"。同时需要一条将来接 `HandleToMaster`（外部 API）的缝。

## 决策

1. `BrainPort` 协议只有两个方法：`complete(BrainRequest) -> BrainReply`、`health() -> bool`。请求/回执都是 frozen dataclass，跨边界时由调用方负责序列化。
2. 本地实现 `LlamaCppBrain` 走 `POST /v1/chat/completions`（OpenAI 兼容），`urllib.request`，超时由配置给（默认 600 s）。
3. 需要结构化输出时传 `response_format={"type":"json_schema","json_schema":{"name":...,"schema":...}}`，交给 llama.cpp 用 GBNF 约束解码；**不**在客户端做"从散文里抠 JSON"。
4. 回执解析失败（`JSONDecodeError`、缺 `choices`、HTTP 非 2xx）统一抛 `BrainError`，由 harness 转成 `ERROR` 信封，不吞异常。
5. `health()` 只做 `GET /health`，用于 `ServerSupervisor` 的就绪判定与排障，不参与业务判断。

## 理由

- 端口与实现分离，`HandleToMaster` 将来只是另一个 `BrainPort` 实现，`Harness` 不用改一行。
- 服务端约束解码把"模型返回非法 JSON"的概率压到接近零，比在客户端写修复逻辑更可靠（也更少的代码）。
- 只暴露 `complete/health`，避免调用方依赖 llama.cpp 特有参数（如 `n_probs`、`grammar`），保住可替换性。

## 代价

- 每次调用是一次 HTTP 往返，比进程内直连多几毫秒。
- `json_schema` 子集受 GBNF 转换能力限制（例如复杂 `oneOf` 可能不被支持），因此 TB-0 的 Plan schema 刻意保持扁平。
- 没有流式输出：TB-0 的 ANSWER 一次性返回，TUI 只能整段出现（后续要流式需扩 `BrainPort`，届时写新 ADR）。

## 验证方式

实跑 `--once "ping"` 时，`runs/server/main.log` 应出现请求日志；返回的 `BrainReply.content` 必须是 `dict`（`LlamaCppBrain.complete` 内断言），否则抛 `BrainError`。
