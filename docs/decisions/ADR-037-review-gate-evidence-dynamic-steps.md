# ADR-037：评审门禁、证据校验与动态步数

- **状态**：已采纳（2026-10-30）
- **实现状态**：已落地并实跑验证（2026-10-30）
- **关联代码**：`agent/core/harness.py`（`run_turn` / `_plan` / `_review_feedback`）、
  `agent/core/learning.py`（`Review` 语义）、`agent/config.py`、`agent/config/defaults.toml`
- **关联 ADR**：ADR-010（计划修复）、ADR-013（评审与报告；advisory 部分由本 ADR 更新）、
  ADR-021（失败预算）、ADR-035（token 预算）
- **上游文档**：`docs/pre_architecture.md` §12；`test_history/README.md`

## 背景

在 Terminal-Bench `jsonl-aggregator`（1M 条记录）上观察到：

1. 主脑计划缺少“验证/修复”步骤，写完代码后直接作答；
2. 次脑评审其实发现了问题，三次都是 `REVISE 0.0`；
3. 但 `Harness.run_turn` 在评审后无条件发出 `ANSWER`，`REVISE/FAIL` 只是报告字段；
4. 最后一轮回答声称 “All tests passed”，ledger 中没有对应的测试 `TOOL_CALL`；
5. `max_steps=4` 固定，评审打回后没有追加步骤的机制。

结论：不是次脑没看见，而是评审没有控制权。

## 决策

1. **评审门禁**：`review.verdict != PASS` 时，若还有 `max_review_repairs` 预算，
   将 review issues/suggestions 作为反馈传给 planner，重新规划并执行；不再直接交付。
2. **重做原因回传**：反馈块包含：
   - `Review verdict`
   - `issues`
   - `suggestions`
   - 最近工具失败（`error.code/message`）
   - 最近 `workspace.run_command` 的非零 `returncode` 与 `stderr`
3. **动态步数**：
   - 基础计划仍为 `max_steps`；
   - 第 N 次打回时 planner 的步数上限为
     `max_steps + N * repair_step_bonus`；
   - 总工具步数上限为
     `max_steps + max_review_repairs * repair_step_bonus`；
   - 默认 `max_review_repairs=2`、`repair_step_bonus=4`，即总上限 12 步。
4. **证据校验**：若 answer 声称
   `tests passed / test suite passed / verified / 测试通过` 等，但工具结果中
   没有 `returncode=0` 且 argv 含 `unittest` / `pytest` / `test` 的
   `workspace.run_command`，则强制把评审改为至少 `REVISE`，并加入 issue。
5. **交付边界**：打回预算耗尽后仍会发出 `ANSWER`，但 `RunReport.review` 保留
   `REVISE/FAIL` 和 issues；不会把失败伪装成 PASS。
6. **test_history 隔离**：`test_history/` 加入 `[guardrail].deny_names`，
   agent 工具不能读取人工保存的期望值/历史结果。

## 理由

- 次脑的职责从“旁路建议”变成“质量门禁”，但仍保留次数上限，避免无限循环。
- 重做原因必须包含工具事实，否则 planner 只会重复原来的错误。
- 证据校验针对最危险的失败模式：**没有测试证据却声称测试通过**。
- 动态步数让验证失败后的修复有预算，而不是被固定 4 步截断。

## 代价与限制

- 评审打回会增加模型调用与角色切换，单轮耗时可能从 ~2 分钟升到 6~7 分钟。
- 门禁只能阻止“无条件交付”，不能保证模型一定改对；Q4 的代码生成方差仍然很大。
- 证据校验只覆盖“测试通过”类声明，不覆盖所有事实性声明。
- 打回预算耗尽后仍交付原文；是否要改成硬失败，留待后续决定。
- `max_review_repairs` / `repair_step_bonus` 对旧配置有默认值，保持向后兼容。

## 验证

### 自动化

```text
python3 tests/run_all.py
Ran 12 tests ... OK
```

新增 `tests/test_review_gate.py`：

- 第一次 `REVISE` 后触发第二次 planner 调用；
- 第二次计划获得追加步数（1 → 2 个工具步）；
- 最终 `ANSWER` 只发一次；
- ledger 含 2 个 `PLAN`、1 个 `RETRY(stage=review)`；
- 证据 issue 合并进 REVIEW（`gate=evidence`）；
- planner 收到 review 原因。

### 实跑

任务：`test_history/2026-10-30-jsonl-aggregator/`。

| 运行 | 结果 |
| --- | --- |
| 门禁前 3 次 | 0/3；评审均 `REVISE 0.0`，但答案仍交付 |
| 门禁后 `t-20261030-204736-5b7959` | 3 个 PLAN、2 次 RETRY、12 步；路径写错，`FAIL` |
| 门禁后 `t-20261030-210137-5cbc6c` | 数值基本正确但 schema key 错；`REVISE 0.5` |
| 门禁后 `t-20261030-210820-0f52fd` | **PASS 1.00，retries=0，120.04 s**；与官方 expected 完全一致 |
| 门禁后 `t-20261030-211034-820a7d` | 默认 2 次打回，429.74 s，仍 `FAIL` |

结论：门禁、动态步数、原因回传均已实际触发；Q4 在 5 次 post-gate 运行中
只有 1 次 exact pass，稳定性仍是模型侧问题。

## 后续

- 将“最后一次写入之后必须有一次成功验证”升级为硬性新鲜度检查；
- 对代码任务增加可选的输出 schema 校验；
- 评估是否把预算耗尽后的交付改为显式 `UNVERIFIED` 标记。

## 2026-10-30 23:12 增补：双语检索、计划限额与回退

针对“软件开发流程”RAG 失败，追加以下策略：

1. **语言策略**：planner 先判断用户语言；`knowledge.search` 的主查询使用用户语言，
   再追加一次另一种语言的同概念查询（中文 ↔ 英文）。最终回答使用用户语言。
2. **计划输出限额**：`_PLAN_MAX_TOKENS=2048`，schema 限制
   `steps.maxItems=12`、`goal/rationale/why` 最大长度，避免重规划时 `n_ctx` 被挤爆。
3. **重规划失败回退**：不再直接 `_fail` 丢弃整轮；保留已有草稿与证据，追加
   “未完成说明/评审意见/重做失败原因/证据数量”，并以较低 confidence 交付。
4. **验证**：
   - `tests/test_review_gate.py` 增至 3 项，总测试 14 项，全绿；
   - 原问题重跑 `t-20261030-231058-ba2b88`：中文查询“软件开发” + 英文查询
     “software development lifecycle”，`REVIEW PASS 1.00`，43.71 s，`retries=0`。
   - 该次回答只列出 3 个步骤：两个查询各 `k=3`，只覆盖了开头和“2. 方案设计”；
     完整 8 步没有被检索到。RAG 覆盖问题仍在，后续可考虑按文档做父级扩展或提高 k。

