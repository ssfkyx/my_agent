# ADR-013：评审（Review）与运行监督报告（RunReport）

- **状态**：已采纳（2026-10-30）
- **关联代码**：`agent/core/learning.py:Reviewer`、`Review`、`Verdict`、`agent/core/report.py:Reporter`、`RunReport`、`agent/core/harness.py:Harness.run_turn`、`Harness._review`
- **关联 ADR**：ADR-005（流水线）、ADR-010（修复重试）、ADR-011（提示契约）、ADR-014（角色路由）、ADR-012（token 预算）
- **上游文档**：`docs/pre_architecture.md` §3 TB-2、§5.3（Review 契约）、§6.2（Reporter）

## 背景

需求是"harness 监督 → 次 agent 评审 → 回主 agent → harness 生成运行监督报告"。
TB-1 之前的 ledger 只有执行事实（计划、工具、答案），没有独立的"第二双眼睛"，
也没有一份可交付的运行报告——事故复盘只能靠人读 JSONL。

同时评审会引入一个风险：**评审失败不能拖垮交付**。次脑要换模型加载，可能超时/崩溃。

## 决策

1. `Review = {verdict, score, issues[], suggestions[]}`，`verdict ∈ {PASS, REVISE, FAIL}`，
   `score` 为 FP64 且被夹到 `[0,1]`；由 `Review.from_payload` 严格校验（枚举越界即 `ReviewError`）。
2. 评审是**建议性（advisory）**的：TB-2 不因 `REVISE/FAIL` 自动返工、也不阻断交付；
   返工策略留给后续切片（需要预算与用户同意，属于 TB-6 的 `Escalator` 领域）。

   > **2026-10-30 更新（ADR-037）**：本条的“advisory”部分已被取代。
   > 现在 `REVISE/FAIL` 会打回 planner 重做，反馈包含 issues/suggestions 和工具失败；
   > 打回次数由 `max_review_repairs` 限制，步数按 `repair_step_bonus` 动态追加。
   > “评审必须在 ANSWER 前”仍然有效。
3. 评审失败（`BrainError`/校验失败）不吞掉：harness 记一条 `ERROR`（`code=UPSTREAM_ERROR`），
   `RunReport.review` 置为 `None` 并在 markdown 中写明"评审不可用"，答案照常交付。
4. `RunReport` 由 harness 在交付前构建并落盘两份：`runs/<turn_id>.report.json`（机读）与
   `runs/<turn_id>.report.md`（人读）。报告含：目标、逐步计划（why）、每次工具调用的 ok/错误码/耗时、
   重试次数、失败清单、答案与置信度、评审结论、总耗时。
5. 事件顺序固定为：`… → REVIEW → ANSWER → RUN_REPORT`。
   改写的理由：评审对象是"草稿答案"，因此 REVIEW 必须在 ANSWER 之前；报告是对整轮的事后总结，放最后。
   旧验收里"ANSWER 是最后一条"的表述由本 ADR 取代（TB-0/TB-1 的历史 ledger 不受影响）。
6. 为让报告有"每步耗时"，harness 在 PLAN / ANSWER 事件的 `meta.elapsed_s` 记录该次模型调用耗时
   （旁路信息，不进 payload；`ToolResult.elapsed_s` 已有工具耗时）。

## 理由

- advisory 而非阻断：TB-2 的次脑是 8B，判断力有限；若它能阻断交付，会把"评审噪声"直接变成用户可见的失败。
- 报告落两份：JSON 给后续反思/记忆消费，Markdown 给人看；两者同源，避免"报告与事实不一致"。
- 评审失败降级为 `ERROR` 事件而不是抛异常，符合 ADR-005"错误也是数据"，也让 TB-6 的升级判断能读到它。

## 代价

- 每轮多一次模型调用（换模型 + 评审），实测约 +8–15 s。
- 评审可能给出与事实不符的结论（8B 幻觉），需要未来用人类反馈校准（TB-4）。
- ledger 尾部不再是 ANSWER，任何依赖"最后一条是 ANSWER"的外部脚本需要改为"找到 ANSWER 即可"。

## 验证方式

- 单测式断言：`Review.from_payload` 拒绝未知 verdict / 越界 score；`RunReport.to_markdown()` 含失败码与重试数；
  评审抛 `BrainError` 时仍返回 ANSWER，且报告 `review=None`、ledger 含 `UPSTREAM_ERROR`。
- 实跑：`--once "list the files in docs/ …"` 的 ledger 顺序为 `… ANSWER → REVIEW → RUN_REPORT`，
  且 `runs/*.report.md` 中的耗时/失败码与 ledger 一致。
