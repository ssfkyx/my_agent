# ADR-014：角色路由（role → alias → 单常驻模型切换）

- **状态**：已采纳（2026-10-30）
- **关联代码**：`agent/ports.py:BrainRequest.role`、`agent/adapters/llama_cpp.py:ModelRouter`、`ModelRouter.complete`、`agent/app.py:App.__init__`
- **关联 ADR**：ADR-002（单常驻 + 文件锁）、ADR-004（BrainPort）、ADR-013（评审需要次脑）
- **上游文档**：`docs/pre_architecture.md` §7 `RoleRegistry`、R5（换卡 3–8 s 已接受）

## 背景

TB-2 引入次脑（LFM2.5）做评审。显存只容一个模型（ADR-002），因此"主脑作答 → 次脑评审"
必然包含一次进程切换。问题在于：**谁负责切换？**

- 若 harness 直接调 `supervisor.ensure("sub")`，core 就依赖了 adapter，分层被打破；
- 若 App 在 run_turn 前后手工 ensure，则调用序列散落在两层，评审失败时容易忘记切回。

## 决策

1. `BrainRequest` 增加 `role: str = "main"` 字段；"用哪个模型"由请求表达，而不是由调用方持有不同 brain 实例。
2. `ModelRouter`（adapters 层）实现 `BrainPort`：
   - 持有 `role → alias` 映射（默认 `main → harness.main_alias`、`review → harness.review_alias`）；
   - `complete(request)` 先 `supervisor.ensure(alias)`，再委托给**按 alias 缓存**的 `LlamaCppBrain`；
   - 未知 role 直接抛 `BrainError`（不静默回落到主脑——否则评审会变成"自己审自己"）。
3. harness 只认识 `BrainPort`：计划/答案用 `role="main"`，评审用 `role="review"`。
4. `App.run_once` 不再显式 ensure 主脑：路由层在第一次调用时按需拉起，`close()` 仍由 App 负责。
5. 切换成本（3–8 s，R5 已接受）由路由承担；同一 role 连续调用不会重复加载。

## 理由

- "角色"是领域概念（planner/executor/reviewer），"alias"是部署概念；请求里写 role 让 core 与具体模型解耦，
  将来 `Handle_to_master` 只是新增一个 role 映射。
- 未知 role 报错而非回落，是因为"评审悄悄用主脑"会产生**看起来通过了评审**的假象——这是最危险的失败模式。
- 缓存 `LlamaCppBrain` 只是为了省构造开销；真正的驻留仍由 supervisor 唯一决定。

## 代价

- 一次 turn 内若在 role 间来回切换，会反复启停进程（TB-2 的顺序是 main → review，只切一次）。
- `BrainRequest` 多了字段，所有构造点需要显式或默认赋值（默认 `"main"` 保持兼容）。
- 路由层持有 supervisor 引用，测试需要假 supervisor 或假 brain。

## 验证方式

- 单测式断言：未知 role 抛 `BrainError`；同一 role 两次调用只 ensure 一次（假 supervisor 计数）。
- 实跑：`--once` 的 server 日志中先出现 `main` 的 `model loaded`，评审阶段出现 `sub` 的 `model loaded`，
  结束后 `nvidia-smi` 回落且无残留进程。
