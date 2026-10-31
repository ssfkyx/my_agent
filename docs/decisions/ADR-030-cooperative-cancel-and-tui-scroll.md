# ADR-030：协作式取消与 TUI 滚动回看

- **状态**：已采纳（2026-10-30）
- **实现状态**：已落地（2026-10-30）
- **关联代码**：`agent/core/harness.py:run_turn/_cancel_outcome`、`agent/app.py:request_cancel/run_session`、
  `agent/ui/web.py:cancel_turn`、`agent/ui/tui.py`（`_scroll` / PageUp/PageDown/Home/End）
- **关联 ADR**：ADR-005（失败作为数据）、ADR-021（ABORTED）、ADR-027（Web UI）、ADR-025（TUI）
- **上游文档**：`docs/pre_architecture.md` §10（错误收敛）、§9（Web/TUI 交互）

## 背景

TUI/Web 的 turn 是同步执行的：模型调用和工具调用都可能持续数十秒。此前用户只能等
turn 结束，或者用 Ctrl-C 结束整个进程；TUI 监督区也只能看最近一屏，无法回看更早事件。
需要一个不引入线程池/异步模型的轻量方案。

## 决策

1. **协作式取消令牌**：`Harness.run_turn(..., cancel: threading.Event | None)`；
   `App` 持有单个 `self._cancel`，`run_once(..., cancel=...)` 透传。
2. **只在检查点取消**：在以下位置检查 `cancel.is_set()`：
   - USER_INPUT 之后、PLAN 之后；
   - 每个工具步骤开始前；
   - ANSWER 生成前后、REVIEW 前后。
   取消时走 `_fail(..., ErrorCode.ABORTED, "cancelled by user")`，发 `ERROR` 并 report。
3. **Web 取消**：`POST /cancel`（需认证）→ `WebUI.cancel_turn()` → `App.request_cancel()`；
   页面在 busy 时显示 Cancel 按钮，SSE 收到 `cancel_requested` 后显示 “cancelling…”。
4. **TUI 取消**：`App.run_session` 仅在 turn 执行期间临时安装 SIGINT handler，
   Ctrl-C 只设置 cancel，不抛 KeyboardInterrupt；turn 结束后恢复原 handler。
   输入状态下 Ctrl-C 仍按原行为退出。
5. **不中断阻塞调用**：当前 `BrainPort.complete` / 工具调用是同步 urllib/subprocess；
   取消只在调用返回后的检查点生效。真正的中断需要 adapter 级取消/流式读取，另行设计。
6. **TUI 滚动**：监督区维护 `_scroll`（距尾部行数，0=跟随）；PageUp/PageDown 按可见行数
   滚动，Home 到最早、End 回尾部；面板标题显示 `+N`。仍只保留最近 120 条事件，
   更早历史在 `runs/*.jsonl`。

## 理由

- 单一 Event + 检查点不改变 Harness 的单线程状态机，也不会在工具/模型调用中间留下半状态。
- Web 的 POST /cancel 与现有 POST /message、POST /consent 对称，页面无需 WebSocket。
- TUI 的临时 SIGINT handler 只在 turn 期间改变 Ctrl-C 语义，输入状态保持原行为。
- 滚动只是渲染窗口偏移，不改变事件存储，风险低。

## 代价与限制

- 取消不是即时的：阻塞中的模型/工具调用必须返回后才会 abort；最坏等待受 `brain_timeout_s`
  约束。
- TUI 只保留 120 条事件、Web 回放 500 条；完整历史仍看 ledger。
- 取消后本轮以 `ERROR(ABORTED)` 结束，不会生成 ANSWER；这是有意的（避免半截答案）。
- 多 session 并发取消仍未实现（单 App/单 turn）。

## 验证方式

- Harness：预置 `cancel` 后 `run_turn` 只发 `USER_INPUT → ERROR(ABORTED)`，
  brain/reviewer/reflector 均不被调用，`ui.report` 收到 ERROR。
- Web：`POST /message` 后 `POST /cancel` 返回 200 且回调被调用；report 后再次 cancel
  返回 409。
- TUI：PTY 下发送 PageUp/PageDown 转义序列 + `/quit`，退出码 0；滚动键不进入输入。
- 页面 JS：`node --check` 通过；Cancel 按钮与 `cancel_requested` 事件处理存在。
