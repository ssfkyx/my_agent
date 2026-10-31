# ADR-021：失败预算、用户同意与 Handle_to_master 升级

- **状态**：已采纳（2026-10-30）
- **实现状态**：已落地（2026-10-30，TB-6 验收通过）
- **关联代码**：`agent/core/budget.py:TurnBudget`、`TurnBudget.record_failure`、`TurnBudget.should_retry`、`agent/core/escalation.py:Escalator`、`Escalator.escalate`、`agent/core/harness.py:Harness._execute_step`、`Harness._escalate`、`agent/ports.py:MasterPort`、`UiPort.ask_consent`、`agent/adapters/handle_to_master.py:HttpMaster`
- **关联 ADR**：ADR-005（顺序流水线，每步两条事件）、ADR-009（护栏拒绝不消耗预算）、ADR-013（评审）、ADR-022（脱敏）、ADR-003（FP64 计时）
- **上游文档**：`docs/pre_architecture.md` §3 TB-6、§10（错误与应对）、R4（接口命名 Handle_to_master）

## 背景

需求规定：工具出错可重试，**尝试次数上限 3**，超过则由 harness 停下并询问用户
"是否调用 Handle_to_master?"；同意则**脱敏后**把原始输入交给 SOTA API，拒绝则终止本轮。

现状（TB-5）里每个计划步骤只执行一次：`ToolResult` 失败即作为数据交给答案模型，
既没有重试，也没有"停下来问人"的通道。

## 决策

1. **重试粒度是"步骤的每次尝试"**：`_execute_step` 对**可重试**错误（`ErrorInfo.retryable=True`）
   重新调用同一工具，最多 `max_attempts=3` 次（含首次）。每次尝试都记一对
   `TOOL_CALL` + `TOOL_RESULT`（对 ADR-005 的"每步两条"作精确化：**每次尝试**两条）。
2. **不可重试错误立即停**（`DENIED_BY_GUARDRAIL`、`BAD_ARGS`、`NOT_FOUND`、`MISSING_MMPROJ`）：
   重试不会改变结果，只会烧时间。
3. 预算耗尽 → 发 `CONSENT_REQUEST`（含原因、尝试次数、脱敏预览），由 `UiPort.ask_consent` 决定：
   - 拒绝 → `ERROR(code=ABORTED)`，state=FAILED，本轮结束（不再调用任何模型）；
   - 同意 → 走 `Escalator`：脱敏 → 调 `MasterPort` → 把回答当作一条额外证据（`TOOL_RESULT` 形状）
     交给答案模型，最终仍走正常的 `ANSWER → REVIEW → REFLECTION → RUN_REPORT`。
4. **升级本身失败**（未配置 / 网络错误）不二次询问：记 `ERROR(UPSTREAM_ERROR)` 并把失败原因交给答案模型，
   让它如实告诉用户"升级也没成功"。
5. `MasterPort` 只暴露 `complete(payload) -> str`；实现（`HttpMaster`）从环境变量取端点与密钥，
   未配置即 `EscalationError("master is not configured")`——**绝不内置任何默认远端地址**。
6. 同意结果进入 ledger（`CONSENT_REQUEST` 的 payload 含 `decision`），便于事后审计"谁批准了外发"。

## 理由

- 只重试可重试错误，是"错误也是数据"原则的延伸：确定性失败重试 3 次只会让用户多等 3 倍。
- 同意是硬前置：外发是唯一越过本机边界的动作，必须先拿到人的许可，且许可本身要留痕。
- 升级结果以 `TOOL_RESULT` 形状回流，复用既有的答案/评审/报告链路，不新增事件类型。
- 端点只从环境读，避免把密钥或第三方地址写进仓库；也让"未配置"成为可解释的显式失败。

## 代价

- 一次 turn 最坏多 2 次工具调用（重试）与 1 次外发调用。
- `ask_consent` 让 `UiPort` 从"只输出"变成"可交互"；非交互模式（`--json`/非 TTY）必须有明确默认（默认 deny）。
- 升级内容仍可能带上未识别的敏感信息（脱敏是规则集，不是保证）。

## 验证方式

- 断言：可重试错误恰好重试 `max_attempts-1` 次后触发 `CONSENT_REQUEST`；拒绝后 `ERROR(ABORTED)` 且无 ANSWER；
  同意且未配置 master 时 `ERROR(UPSTREAM_ERROR)` 仍交付答案；`DENIED_BY_GUARDRAIL` 不重试。
- 实跑：`--consent deny` 的越权请求仍应立刻 `DENIED_BY_GUARDRAIL`（不消耗预算）。
