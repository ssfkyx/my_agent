# ADR-002：单常驻模型 + 进程级文件锁

- **状态**：已采纳（2026-10-29）
- **关联代码**：`agent/adapters/llama_cpp.py:ServerSupervisor`、`ServerSupervisor.ensure`、`ServerSupervisor.stop`、`ServerSupervisor._acquire_lock`、`agent/app.py:App.run_once`
- **关联 ADR**：ADR-004（模型调用方式）、ADR-006（TB-0 范围）
- **上游文档**：`docs/pre_architecture.md` ADR-1、§11 R5

## 背景

- 显存 12282 MiB，桌面已占 ~1178 MiB；主模型 `gemma4-v2-Q6_K` 权重 9.1 GiB，次模型 6.5 GiB。**两者不可能共存**。
- 需求为"顺序流水线，一次只 call 一个模型"。
- 用户明确警告：**同时调用两个模型会杀死进程**（OOM Killer / 驱动复位）。这不是"性能问题"，是"进程存亡问题"。

## 决策

1. `ServerSupervisor` 是唯一允许拉起 `llama-server` 的组件；`ensure(alias)` 先 `stop()` 现有进程，再启动目标模型，阻塞到 `/health` 返回 `ok`。
2. 用 `fcntl.flock(LOCK_EX | LOCK_NB)` 锁 `runs/model.lock`，锁随 supervisor 生命周期持有；抢锁失败抛 `ModelBusyError`，**不降级、不排队**。
3. 进程以 `start_new_session=True` 启动，日志重定向到 `runs/server/<alias>.log`；`stop()` 走 `terminate → wait(15s) → kill`。
4. 启动参数默认带 `--fit on`（llama.cpp 自行按显存下调参数），除非显式配置了 `n_gpu_layers >= 0`。
5. `App.close()` 在 `finally` 中调用，保证异常路径也释放显存。

## 理由

- 文件锁而不是内存标志：即使你手滑开了两个 `python -m agent`，第二个会立刻失败而不是把机器打挂——这正是"会杀死进程"的防线。
- `ensure` 语义（而不是 `start`）让调用方无需关心当前驻留谁，顺序流水线天然只用到一个 brain。
- `--fit on` 把"9.1 GiB 权重 + KV cache 是否塞得进 11 GiB"这类算术交给 llama.cpp，避免我手算层数写死。

## 代价

- 主↔次切换需重启进程（3–8 s，R5 已接受）。
- 单实例限制：不能并行跑两个 turn（与"顺序流水线"一致）。
- 抢占失败只报错不重试，调用方必须自己决定是否提示用户。

## 验证方式

实跑 `python -m agent --once "ping"`，观察 `runs/server/main.log` 出现 `model loaded`；结束后 `nvidia-smi` 回落到基线且无残留 `llama-server`。
