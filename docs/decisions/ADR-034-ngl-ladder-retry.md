# ADR-034：llama-server 启动的 ngl 降档重试

- **状态**：已采纳（2026-10-30）
- **实现状态**：已落地并实跑验证（2026-10-30）
- **关联代码**：`agent/config.py:ModelProfile.gpu_layers_ladder`、
  `agent/adapters/llama_cpp.py:ServerSupervisor._start`、
  `agent/adapters/llama_cpp.py:ServerSupervisor._terminate_process_tree`、
  `agent/adapters/llama_cpp.py:ServerSupervisor.gpu_layers` / `snapshot`、
  `agent/ports.py:RuntimeInfoPort`、`agent/core/harness.py`、`agent/core/report.py`
- **关联 ADR**：ADR-002（单常驻模型）、ADR-033（Q4 主脑）
- **上游文档**：`docs/pre_architecture.md` §10（错误与应对）、§12（进度）

## 背景

WSL2 + 12 GB 卡上，llama-server 的失败经常发生在**连续缓冲分配**，而不是"总显存不够"：

- 同一个模型、同一份权重，`-ngl 20` 报 `cudaMalloc failed: out of memory`，`-ngl 16` 却能正常启动；
- 权重、KV cache、计算图缓冲的分配顺序导致碎片化，失败具有随机性；
- 进程 OOM 后会**提前退出**，若 supervisor 只等到健康检查超时，会浪费整个 `health_timeout`，
  且旧进程可能残留显存，让下一次尝试继续失败。

需求是"启动失败要能自动降档重试，并且明确告诉用户最终用了多少层"，而不是静默降级或一次判死。

## 决策

1. **阶梯定义在配置对象上**：`ModelProfile.gpu_layers_ladder()` 返回尝试序列：
   - `n_gpu_layers is None`（配置 `-1`）→ `(--fit on, 32, 28, 24, 20, 16, 12, 8, 4, 0)`；
   - 显式 `N` → `(N,) + (32, 28, 24, 20, 16, 12, 8, 4, 0)` 中所有 `< N` 且不重复的档位。
   - 例：`99` → `(99, 32, 28, 24, 20, 16, 12, 8, 4, 0)`（10 档）。
2. **每次尝试独立启动**：`ServerSupervisor._start` 为每一档重新 `Popen`，日志追加
   `=== start attempt i/n: -ngl N ===`，便于事后定位是哪一档失败。
3. **早退立即判失败**：轮询中若 `proc.poll() is not None`，立即记录日志尾部并调用 `stop()`，
   不空等健康检查超时；这是降档能及时触发的前提。
4. **按进程组清理**：`start_new_session=True` 拉起，`stop()` 用
   `os.killpg(SIGTERM)` → 超时 `os.killpg(SIGKILL)`；若 pgid 等于自身进程组则退化为
   只终止 `proc`，避免误杀调用方。这样旧进程不会带着显存进入下一档。
5. **成功要可观测**：健康检查通过后记录实际 ngl 到 `Supervisor.gpu_layers`，并在日志写
   `=== ready: <alias> -ngl N (attempt i/n) ===`；全部失败时抛 `RuntimeError`，
   错误信息包含完整尝试序列与日志尾部。
6. **不自动改配置**：降档只影响本次进程，不回写 `defaults.toml`；避免一次偶发 OOM
   永久降低部署规格。
7. **报告可追溯且按轮统计**：`ServerSupervisor.begin_turn()` 清空本轮集合；每次
   `ensure(alias)` 记录 `alias → 实测 ngl`（含复用已驻留模型的情况）；
   `snapshot()` 只返回本轮使用过的模型。`Harness` 通过 `RuntimeInfoPort` 注入，
   `RunReport` 的 JSON 与 Markdown 增加 `models` 段。这样多轮 Web/TUI 会话中，
   每份报告只反映该轮实际用过的模型，而不是进程内的历史累计。

## 理由

- 在 12 GB 卡上，`-ngl 99` 首次成功是理想路径，降档是现实兜底；两者都需要。
- 早退检测 + 进程组清理把"失败一次"的成本从"等满超时 + 残留显存"降到"立即重试"。
- 显式回传实际 ngl 满足"不要静默降级"的要求；模型/用户都能从日志和报告追溯。

## 代价与限制

- 最坏情况会启动 10 次 llama-server；每次都要重新加载 6.9 GiB 权重，耗时可能达数分钟。
  这是"最终能跑起来"换来的代价，健康超时应保持合理（当前配置值）。
- 阶梯是静态列表，不感知实时显存；若宿主有其他进程占卡，所有档位都可能失败。
- 降档到很低层数时推理会变慢，但不会自动切换到更小模型；跨模型回退仍由 ADR-007 的
  `llama31` profile 人工决定。
- OOM 判定的依据是进程退出/健康检查失败，不解析具体 CUDA 错误文本；未来可细化。

## 验证方式与结果

**静态断言（2026-10-30）**

- `compileall` 通过；
- `ModelProfile(n_gpu_layers=99).gpu_layers_ladder()` == `(99, 32, 28, 24, 20, 16, 12, 8, 4, 0)`；
- `n_gpu_layers=None` → 首档为 `None`（`--fit on`），后续为 32…0；
- `/tmp/test_terminate.py` 验证进程组终止与幂等：`process-tree termination: OK`。

**实跑（2026-10-30 18:11 CET）**

| # | 检查 | 结果 |
| --- | --- | --- |
| 1 | `python3 -m agent --once "ping" --json` | 成功，33.47 s，`retries=0 failures=0` |
| 2 | `runs/server/main.log` | `start attempt 1/10: -ngl 99` → `model loaded` → `ready: main -ngl 99 (attempt 1/10)` |
| 3 | 生成速度 | 41.80 tok/s，说明 99 层确实 offload 成功 |
| 4 | 退出清理 | 运行结束后无残留进程，显存回到约 940 MiB |
| 5 | 失败路径（单元级） | 子进程提前退出时立即 `stop()` 并进入下一档，不等待健康超时 |
| 6 | `RunReport` 模型段（18:18 CET） | `t-20261030-181800-13cdd1` 的 JSON `models={'main': 99, 'sub': 99}`；Markdown 有 `## Models` 表 |
| 7 | 按轮统计（18:28 CET，同一 Web 进程两轮） | 第 1 轮 `t-20261030-182753-a554bc`：`models={'main': 99, 'embed': None, 'sub': 99}`；第 2 轮 `t-20261030-182845-5985d6`：`models={'main': 99, 'sub': 99}`，`embed` 不再出现 |
| 8 | 单元级按轮统计 | `/tmp/test_turn_models.py`：`begin_turn` → `ensure` → `snapshot` 在切换轮次与复用驻留模型时均正确：`per-turn model accounting: OK` |

> 首次成功路径已实测；"连续多档失败直到 CPU 0 层"未做真实 OOM 注入，
> 由 `_start` 的控制流与 `/tmp/test_terminate.py` 覆盖。
