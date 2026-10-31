# ADR-015：结构化调用关闭思考通道（评审请求）

- **状态**：已采纳（2026-10-30）
- **关联代码**：`agent/ports.py:BrainRequest.disable_thinking`、`agent/adapters/llama_cpp.py:LlamaCppBrain.complete`、`agent/core/learning.py:Reviewer.review`
- **关联 ADR**：ADR-012（思考通道吃掉 token 预算）、ADR-004（BrainPort 契约）、ADR-013（评审）、ADR-014（角色路由）
- **上游文档**：`docs/pre_architecture.md` §5.3

## 背景

TB-2 第一次实跑评审成功（`PASS 1.00`），第二次却失败：
`review unavailable: response message.content is empty` → harness 按 ADR-013 降级为
`ERROR(UPSTREAM_ERROR)`、报告 `review=n/a`、答案照常交付（降级路径正确）。

失败原因与 ADR-012 同源但方向相反：评审用的是 **LFM2.5**，它也有推理通道；评审 prompt
包含计划 + 全部工具结果，比计划/答案的 prompt 长得多，1024 token 的配额可能先被推理吃光。

而"评审"这个任务恰恰**不需要长思考**：输出只有 `verdict/score/issues/suggestions` 四个字段。

## 决策

1. `BrainRequest` 增加 `disable_thinking: bool = False`；为 True 时适配器在请求体里加
   `chat_template_kwargs: {"enable_thinking": false}`（llama.cpp 会把它传给模型模板，模板不支持则无副作用）。
2. 只有 `Reviewer.review` 置 `disable_thinking=True`：主脑的计划/答案仍保留思考通道（质量优先）。
3. 不在配置里暴露该开关：这是"结构化短输出"的语义约束，不是部署参数；主脑若将来也需要，再单开 ADR。
4. 评审失败仍走 ADR-013 的降级路径，不做自动重试（重试会把 8B 的噪声放大成延时）。

## 理由

- 评审的输出空间极小（4 个字段），思考的边际收益接近零，而失败成本是"整轮没有评审"。
- 用请求级字段而不是全局开关，是为了让"哪些调用不需要思考"成为一个可评审的、写在代码里的决定。
- 不改 `max_tokens` 继续上调，是因为它会同时放大所有调用的最坏延时（ADR-012 已记过这笔账）。

## 代价

- 评审质量可能略降（少了显式推理），但 8B 的推理本身噪声大，收益不确定。
- 依赖 llama.cpp 的 `chat_template_kwargs` 透传；若某模型模板不识别 `enable_thinking`，该字段被忽略
  （不会报错），此时行为回落到"带思考"，需重新评估 token 预算。

## 验证方式

- 单测式断言：`Reviewer.review` 发出的 `BrainRequest.disable_thinking` 为 True，且主脑请求为 False。
- 实跑：连续 3 次 `--once` 的评审都不再出现 `content is empty`；报告 `review` 字段非 null。
- 若仍失败，说明是 prompt 过长而非思考通道，届时改走"裁剪工具结果"的路线（另开 ADR）。
