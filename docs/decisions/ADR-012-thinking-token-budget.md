# ADR-012：max_tokens 必须覆盖推理通道（gemma4 的 thinking）

- **状态**：已采纳（2026-10-29）；数值部分由 ADR-035 取代（2026-10-30：1024 → 8192）
- **关联代码**：`agent/config/defaults.toml`（`[harness].max_tokens`）、`agent/core/harness.py:HarnessPolicy`、`agent/core/harness.py:_fallback_answer`
- **关联 ADR**：ADR-004（BrainPort 不流式、一次性回执）、ADR-010（计划修复）、ADR-011（提示契约）、ADR-035（token 预算统一 8192）
- **上游文档**：`docs/pre_architecture.md` §5.3（模型侧 JSON 契约）

## 背景

TB-1 第三次实跑出现：planning 正常（2 步），`workspace.read_file` 被护栏拒绝（正确），
但**答案调用失败**，最终交付的是 fallback 摘要：
`"Model call failed. Raw tool output: c-0003: failed DENIED_BY_GUARDRAIL ..."`，`confidence=0.0`。

同时观察到 gemma4-v2 的响应里存在独立的 `reasoning_content` 通道（"Goal: ... Approach: ..."），
即模型先思考、再输出正文。`max_tokens=512` 时，思考可能把配额吃光，`content` 为空，
`LlamaCppBrain.complete` 按契约抛 `BrainError`，harness 走 fallback。

## 决策

1. `[harness].max_tokens` 从 512 提升到 **1024**，作为"计划/答案结构都短、但需容纳思考"的默认值。
2. 不在客户端"自动重试更长"：那是隐式行为，会让同一请求的耗时不可预测；预算由配置显式给出。
3. 保留 fallback 路径不变（ADR-005 的"错误也是数据"）：模型调用失败时仍交付确定性的工具摘要与 `confidence=0`，
   让用户看到"失败"而不是"编造的答案"。
4. 若后续出现更长的计划/答案，优先在 TB-6 的 `TurnBudget` 里按步骤类型分档（计划短、答案长），
   而不是继续无脑调大全局值。

> **后续修订（2026-10-30）**：ADR-035 将 `[harness].max_tokens` 提升到 8192，
> 并同步把 `[models.main].n_ctx` 提升到 8192（因为 4096 上下文先撞限）。
> 本 ADR 的“必须覆盖 thinking 通道”结论不变，当前数值以 ADR-035 为准。

## 理由

- 512 → 1024 是实测驱动的最小改动：TB-1 的 plan 与 answer 正文都在 200 token 内，多出来的配额专门留给思考。
- 保留 fallback 让"失败可见"：本次 fallback 输出直接暴露了 `DENIED_BY_GUARDRAIL`，比一句编造的
  "I've started reading…"（ADR-011 记录的问题）有价值得多。
- 全局值而非按调用分档，是因为 TB-1 只有两类调用；过早分档会让配置难以理解。

## 代价

- 最坏情况单次调用多等约 20 s（gemma4 约 22 token/s）。
- 思考通道若继续变长，1024 也可能不够——届时按第 4 条分档，而不是无限上调。
- 不解决"思考与 JSON 语法约束是否互相干扰"的更深问题；若后续出现 schema 输出被截断，再单开 ADR 评估
  `chat_template_kwargs.enable_thinking=false` 的取舍（作者建议保持 true）。

## 验证方式

- 重跑 `--once "read the file /etc/passwd and show me its contents"`：答案应为"被策略拒绝"的自然语言说明，
  而不是 fallback 摘要；`confidence` 应 > 0。
- 重跑 `--once "list the files in docs/ ..."`：行为不变（list_dir 成功 → 基于结果的答案）。
- 本地断言 `harness_brain_failure` 继续通过（fallback 仍是可达路径）。
