# ADR-017：嵌入服务作为独立 role（embed），与主脑互斥

- **状态**：已采纳（2026-10-30）
- **关联代码**：`agent/adapters/embeddings.py:LlamaCppEmbedder`、`LlamaCppEmbedder.embed`、`agent/config/defaults.toml`（`[models.embed]`）、`agent/app.py:App.__init__`
- **关联 ADR**：ADR-002（单常驻）、ADR-014（role 路由）、ADR-016（RAG）、ADR-003（FP64）
- **上游文档**：`docs/pre_architecture.md` §11.1（embed alias / 端口 8083）、ADR-4

## 背景

RAG 需要把文本变成 1024 维向量。嵌入模型（Qwen3-Embedding-0.6B）与主脑（gemma4 12B）不能同时在卡上
（ADR-002），所以"检索"这一步必然触发一次模型切换。问题：**嵌入走哪条通道？**

- 走 `ModelRouter`（`role="embed"`）？但 `BrainPort` 的契约是"给消息返回 JSON"，嵌入不是 chat，
  硬塞会让 `BrainRequest` 长出互相矛盾的字段。
- 走独立的 `EmbedderPort` 实现：语义清晰，但与 supervisor 的交互需要复述一遍"ensure"。

## 决策

1. 新增 `agent/adapters/embeddings.py:LlamaCppEmbedder`，实现 `EmbedderPort`（`embed(texts) -> list[vector]`），
   通过 `POST /v1/embeddings` 调用**已 ensure 的 embed 模型**。
2. `LlamaCppEmbedder` 持有 `ServerSupervisor` 并调用 `ensure("embed")`；与 `ModelRouter` 共用同一把
   进程锁，因此"嵌入"与"对话"天然互斥，不存在并发加载。
3. 嵌入请求的批大小由调用方（`KnowledgeBase`）控制（默认 16 条/批），避免一次塞入过多文本。
4. 返回向量用 `array("d")`（FP64），与 ADR-016 的存储精度一致。
5. 索引/检索期间不得同时发起对话调用：一次 turn 内若先 `knowledge.index` 再让主脑作答，
   顺序必然是"主脑加载 → 嵌入加载 → 主脑再加载"，代价约 3–8 s/次切换（R5 已接受）。

## 理由

- 把"嵌入"与"对话"分成两个端口，是为了让 `BrainPort` 保持"只做 chat"的单一语义；
  否则 `BrainRequest.messages/json_schema` 在嵌入场景下全是噪声。
- 共用 supervisor 而不是另起进程管理，保证"同一时刻只有一个 llama-server"这条铁律只有一处实现。
- 批处理是嵌入 API 的天然形态，也减少 HTTP 往返。

## 代价

- 检索一次 = 一次模型切换；若未来频繁检索，需要"常驻小模型 + 主模型按需"的两卡方案（硬件前提不满足，故不做）。
- `EmbedderPort` 只暴露 `embed`，没有 `health`；健康判断复用 supervisor 的就绪语义。
- 嵌入服务若返回维度不一致（例如换了模型），`SqliteVectorStore` 会在 `dim` 列上暴露差异，需要重建索引。

## 验证方式

- 单测式断言：假 supervisor 被调用 `ensure("embed")`；返回向量长度等于响应维度。
- 实跑：`index docs` 成功后 `dim=1024`；`search` 的 top-1 命中 `docs/` 且 score ∈ [-1,1]。
