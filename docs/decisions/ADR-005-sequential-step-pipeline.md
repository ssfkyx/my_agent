# ADR-005：Step 顺序流水线（TOOL / ANSWER）

- **状态**：已采纳（2026-10-29）
- **关联代码**：`agent/core/plan.py:Plan`、`Plan.from_payload`、`Step`、`StepKind`、`agent/core/harness.py:Harness.run_turn`、`Harness._execute_tool`、`Harness._answer`
- **关联 ADR**：ADR-001（信封）、ADR-006（TB-0 只实现两种 Step）
- **上游文档**：`docs/pre_architecture.md` §4 状态机、§6.1 工具目录

## 背景

需求是"主 agent 分解任务 → 选工具 → 完成执行计划（意图 + 分类排序 + 决策理由）→ 执行 → 监督 → 评审 → 报告 → 反思"。TB-0 只需证明这条链最薄的一段能跑通；若一开始就上 DAG/并行，失败点会同时来自并发与时序。

## 决策

1. `Plan = {goal, rationale, steps[]}`；`Step = {step_id, kind, target, args, why}`。`why` 必填——"记录决策原因"是需求硬要求，因此在 schema 层强制。
2. `StepKind` 只有 `tool` 与 `answer`；**执行严格串行**：按 `steps` 顺序，遇到 `answer` 即收尾，未执行的步骤不算失败（记录为未执行）。
3. 工具调用前后各产生一个信封（`TOOL_CALL`、`TOOL_RESULT`），因此每个工具步骤固定贡献 2 条 ledger 事件——这是 TB-0 验收"≥5 事件"的算术依据（USER_INPUT + PLAN + 2×TOOL + ANSWER）。
4. 工具异常不得击穿 harness：`Tool.invoke` 必须返回 `ToolResult(ok=False, error=...)`；harness 只负责把失败结果转成信封并继续/收尾。
5. `max_steps` 由配置给定，超出即截断并记 `ERROR`，防止模型给出超长计划。

## 理由

- 串行是最小可验证语义：每一步的输入只依赖前一步的产物，ledger 顺序即因果顺序，排障时不必重建并发时序。
- `why` 强制存在，使 RunReport / 反思阶段能直接引用"当时的理由"，而不是事后编故事。
- 失败以 `ToolResult` 承载而非抛异常，保证"错误也是数据"，可在 UI 与报告中同样渲染。

## 代价

- 独立步骤无法并行，长计划会慢于 DAG 执行（与"顺序流水线"需求一致，不是缺陷）。
- 每个工具步骤两条事件，ledger 体积约为步骤数的两倍。
- `answer` 之后的步骤被静默跳过，需要 `RunReport` 显式说明未执行（TB-2 落地）。

## 验证方式

`--once "ping"` 的 ledger 中 `TOOL_CALL` 与 `TOOL_RESULT` 必须成对且 `call_id` 相同；`ANSWER` 必为最后一条。
