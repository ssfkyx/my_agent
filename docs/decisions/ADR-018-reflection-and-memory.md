# ADR-018：反思（Reflection）与长期记忆（只追加的 JSONL）

- **状态**：已采纳（2026-10-30）
- **关联代码**：`agent/core/reflection.py:Reflector`、`Reflection`、`agent/adapters/memory.py:JsonlMemoryStore`、`agent/tools/memory.py:RecallTool`、`RecordTool`、`RequestToolChangeTool`、`agent/core/harness.py:Harness._reflect`
- **关联 ADR**：ADR-013（评审）、ADR-014（role 路由）、ADR-015（关闭思考）、ADR-011（提示契约）
- **上游文档**：`docs/pre_architecture.md` §3 TB-4、§6.2（记忆大类）

## 背景

需求："每次做事后按次 agent 评估与人类反馈总结计划方式与函数调用的可提升点，积累到长期记忆；
想改函数则向 harness 提需求"。TB-2 有了评审，但评审结论只落在单轮报告里，**下一轮完全不知道**。

## 决策

1. `Reflection = {lessons[], plan_hint, tool_notes[]}`，由次脑（`role="review"`，已驻留，免二次换模型）
   在评审之后生成；输出走 JSON schema，`disable_thinking=True`（同上一条 ADR 的理由）。
2. 记忆用 **JSONL 只追加**：`runs/memory.jsonl`，每条含 `turn_id/ts/verdict/lessons/plan_hint/tool_notes`。
   禁止重写或压缩（历史不可篡改是审计前提）。
3. harness 在**下一轮的计划提示词**里注入最近 `recall_limit`（默认 3）条 `lesson` 与 `plan_hint`，
   并在 `PLAN` 事件的 `meta.lessons` 记录注入条数——这样"记忆是否真的影响计划"可被外部核验。
4. 反思是 **best-effort**：失败时不阻断、不记 ERROR（否则一次模型抖动会被计成运行失败）；
   该轮的 `RUN_REPORT.memory` 记为 0 条。
5. 工具变更请求只**入队**：`memory.request_tool_change(tool, reason)` 写入 `runs/tool_requests.jsonl`，
   不会自动改工具/提示词；主 agent 想改函数时只能经由这个队列向 harness 提需求（人审后再生效）。
6. 事件顺序：`… → REVIEW → REFLECTION → ANSWER → RUN_REPORT`。

## 理由

- 只追加 + 独立文件，让"记忆"可以被 diff、grep、回滚，不需要数据库迁移。
- 注入的是 `lessons`（短句）而不是整条反思：计划提示长度可控，且避免把旧结论当事实灌进新任务。
- 变更请求入队而不生效，是"agent 不自改自身权限"的底线；否则一个坏反思就能悄悄放宽护栏。

## 代价

- 每轮多一次模型调用（次脑已驻留，实测约 +4–6 s）。
- 记忆无淘汰机制：文件会线性增长（tens of KB/百轮），检索时只取最近 N 条，暂不需要清理策略。
- 反思可能复述评审（信息冗余）；由 `lessons` 的去重与长度上限（每条 <= 200 字符）缓解。

## 验证方式

- 单测式断言：`Reflection.from_payload` 拒绝空 lessons 之外的非法结构；JSONL 追加两次后行数只增；
  `request_tool_change` 只写队列文件、不触碰工具注册表。
- 实跑：连续两轮 `--once`，第二轮 `PLAN.meta.lessons >= 1`，且 `runs/memory.jsonl` 行数随轮次增长。
