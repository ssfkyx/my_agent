# ADR-010：计划非法时的 1 次修复重试（并新增 RETRY 事件）

- **状态**：已采纳（2026-10-29）
- **关联代码**：`agent/core/harness.py:Harness._plan`、`Harness._call_planner`、`agent/core/protocol.py:MessageKind`、`agent/ui/presenter.py:Presenter._detail`
- **关联 ADR**：ADR-004（schema 约束解码）、ADR-005（流水线）、ADR-006（kind 集合的裁剪与扩展规则）、ADR-009（护栏）
- **上游文档**：`docs/pre_architecture.md` §3 TB-1 行（"坏 JSON 触发 1 次重试"）、§6 错误与应对

## 背景

TB-0 的计划来自 `json_schema` 约束解码，理论上必为合法 JSON；但 schema 只能约束**形状**，不能约束语义：
`steps` 里出现未知 `kind`、`tool` 步骤缺 `target`、`args` 类型不对，都会让 `Plan.from_payload` 抛错。
TB-1 的验收明确要求"坏 JSON 触发 1 次重试后成功或失败"。

同时，"重试"这件事必须对监督可见——否则用户只看到一次成功的计划，无法知道模型第一次犯错。

## 决策

1. `Harness._plan` 在 `Plan.from_payload` 抛 `ProtocolError` 时，**只重试一次**：
   把上一次的原始输出作为 `assistant` 消息、把校验错误作为 `user` 修复提示，重新请求；
   第二次仍失败则不再重试，直接走 `ERROR(PLAN_INVALID)` 收尾。
2. 重试次数写死为 1（常量 `_PLAN_REPAIR_LIMIT = 1`），不进配置：TB-1 只需要区分"能修"与"不能修"，
   可调的重试预算属于 TB-6 的 `TurnBudget`，届时由预算层统一管理。
3. 每次重试发一条 `MessageKind.RETRY` 信封（payload：`reason`/`attempt`/`limit`/`stage="plan"`），
   UI 用 `warn` 色显示；`RETRY` 只表示"发生了修复动作"，不代表最终结果。
4. `MessageKind` 扩展遵循 ADR-006 的"只增不改"：新增 `RETRY`，旧 ledger 文件仍可解析。
5. 重试**不覆盖**网络/服务错误（`BrainError`）：那是环境问题，重试只会放大等待；仍按 `PLAN_INVALID` 之外的原路径处理。

## 理由

- 一次修复重试的收益/成本最划算：绝大多数非法计划是"漏字段/枚举写错"，给出具体错误后模型能改对；
  继续重试则容易在同一处反复打转（也让"3 次失败升级"的语义提前被消耗）。
- 把 `RETRY` 变成一等事件，是为了让 TB-2 的 Review 与 TB-6 的升级判断能引用"模型改过几次"，
  而不是靠解析日志文本。
- 重试提示只回填"校验错误 + 上次输出"，不注入额外指令，避免把修复变成"猜用户意图"。

## 代价

- 最坏情况多一次完整推理（TB-1 实测 gemma4 单次计划 ~8 s，可接受）。
- ledger 会多一条 `RETRY`，`seq` 相应后移，工具 `call_id` 也随之变化（本就用 `next_seq` 派生，无需改动）。
- 修复请求会重复携带系统提示，prompt token 翻倍（TB-1 的 prompt 很短，暂不优化）。

## 验证方式

- 单测式断言：假 brain 第一次返回非法 Plan、第二次返回合法 Plan → 结果成功且 ledger 含 1 条 `RETRY`；
  连续两次非法 → `ERROR(PLAN_INVALID)`，且 `RETRY` 恰好 1 条（不无限重试）。
- 实跑：正常 `--once "ping"` 不应出现 `RETRY`（说明 schema 约束下计划本身合法）。
