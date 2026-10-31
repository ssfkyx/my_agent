# ADR-023：角色切换工具与 ROLE_SWITCH 事件

- **状态**：已采纳（2026-10-30）
- **实现状态**：已落地（2026-10-30，TB-7 验收通过）
- **关联代码**：`agent/ports.py:RolePort`、`RoleError`、`agent/adapters/llama_cpp.py:RoleRegistry`、
  `RoleRegistry.resolve`、`RoleRegistry.switch_role`、`ModelRouter`、`agent/tools/delegate.py:SwitchRoleTool`、
  `agent/core/harness.py:Harness._active_role`、`Harness._emit_role_switch`、`agent/core/protocol.py:MessageKind.ROLE_SWITCH`
- **关联 ADR**：ADR-014（role 路由）、ADR-002（单常驻模型）、ADR-005（顺序流水线）、ADR-001（信封）
- **上游文档**：`docs/pre_architecture.md` §3 TB-7、§6.1（delegate 大类）、R6（ROLE_SWITCH）

## 背景

TB-2 已经有 role 路由，但 role 是**调用方写死**的：计划/答案固定 `main`，评审固定 `review`。
TB-7 要求允许在一次 Turn 内切换主脑身份，且：

- 不新开 Turn、不清空上下文；
- ledger 必须记录 `ROLE_SWITCH` 事件；
- 切换后下一次模型调用走新模型。

问题同样是分层：工具不能直接 import `ModelRouter`/`ServerSupervisor`，core 也不能依赖 adapter；
同时“切换”是工具副作用，必须留下可审计事件。

## 决策

1. **adapter 持有当前角色**：`RoleRegistry` 保存 `role → alias` 映射与 `current`。
   `resolve` 只查询；`switch_role` 只改 `current`，**不加载模型**。真正的换卡由下一次
   `ModelRouter.complete` 调 `supervisor.ensure` 完成（ADR-014/002）。
2. **core 只认识 `RolePort`**：新增 `RolePort` 协议（`current` / `resolve` / `switch_role`）。
   Harness 用它决定计划/答案用哪个 role；工具用它执行切换。core 不 import adapter。
3. **工具名固定 `delegate.switch_role`**：参数 `role` 的 schema 限定为 `main | review`；
   未知角色返回 `BAD_ARGS`，**不回落 main**。切换不触发重试、不消耗失败预算。
4. **只影响“后续主脑调用”**：Harness 的 planner/answer 使用 `RolePort.current`；
   Reviewer/Reflector 仍固定 `role="review"`（ADR-014），避免评审身份被切换污染。
5. **事件由 Harness 落账**：工具成功后，Harness 在 `TOOL_RESULT` 之后追加一条
   `ROLE_SWITCH`，payload 为 `{from, to, alias, changed}`，`meta.role` 为新角色。
   工具本身不写 ledger（工具只返回 ToolResult）。
6. **兼容旧构造**：`ModelRouter` 仍接受 `roles=` 映射；新代码传 `registry=`。两者都没有时明确报错。

## 理由

- “当前角色”是运行期状态，放在 adapter 的 `RoleRegistry` 最贴近模型路由；core 只通过端口读取，
  保持分层。
- 懒切换避免“工具一调用就换卡”把一次工具调用变成 3–8 s 的阻塞；也让同一 Turn 的多个工具步骤
  不会无意义地反复启停模型。
- 事件由 Harness 写，保证 `ROLE_SWITCH` 与 `TOOL_CALL/TOOL_RESULT` 的顺序稳定可审计；
  工具只负责业务副作用，不越权写流水。
- 未知角色直接失败而不是回落，延续 ADR-014 的原则：**静默回落比报错更危险**。

## 代价

- 切换后的第一次模型调用才承担换卡成本；如果计划在切换后没有模型调用，成本为零。
- 角色集合目前来自配置的 `main_alias` / `review_alias`，新增角色需要同时改配置与 `RoleRegistry` 构造。
- 切换状态在进程内持续到下一次切换/进程结束；没有“本 Turn 结束自动还原”。这与“不新开 Turn”一致，
  但多轮 TUI 使用时需要显式切回。
- `ROLE_SWITCH` 只表示 registry 状态改变，不证明模型已加载；加载证明看 `runs/server/<alias>.log`。

## 验证方式

- 断言：`RoleRegistry` 的 resolve/switch/unknown-role；`SwitchRoleTool` 成功与 `BAD_ARGS`；
  Harness 中 planner 用 `main`、切换后 answer 用 `review`，ledger 恰好一条 `ROLE_SWITCH`
  且位于 `TOOL_RESULT` 之后；同一 turn_id；`ModelRouter` 按 alias 懒 ensure 且未知 role 不 ensure。
- 实跑：`First call delegate.switch_role with role=review...`（turn `t-20261030-124457-a2cc2a`）
  - ledger：`TOOL_CALL delegate.switch_role` → `TOOL_RESULT ok` → `ROLE_SWITCH main -> review alias=sub`；
  - `runs/server/main.log` 与 `runs/server/sub.log` 均有 `model loaded`；
  - `retries=0 failures=0 review=PASS`，答案在切换后由 review 角色给出。
