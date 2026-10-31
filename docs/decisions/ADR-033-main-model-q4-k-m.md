# ADR-033：主脑切换为 gemma4-v2-Q4_K_M

- **状态**：已采纳（2026-10-30）
- **实现状态**：已落地并实跑验证（2026-10-30）
- **关联代码**：`agent/config/defaults.toml`（`[models.main]`）、
  `agent/config.py:ModelProfile.gpu_layers_ladder`、`agent/adapters/llama_cpp.py:ServerSupervisor._start`
- **关联 ADR**：ADR-008（模型完整性）、ADR-002（单常驻模型）、ADR-034（ngl 降档）
- **上游文档**：`docs/pre_architecture.md` §0（环境事实）、§11.1（模型清单）、§12（进度）

## 背景（决定性证据）

Q6_K 主脑在 RTX 4080 Laptop（12282 MiB）上无法完成上下文创建：

```
allocating 1088.00 MiB on device 0: cudaMalloc failed: out of memory
failed to allocate CUDA0 buffer of size 1140850688
llama_init_from_model: failed to initialize the context:
    failed to allocate buffer for kv cache
```

- 失败点不是权重加载，而是**权重加载完成后 KV cache 的连续显存分配**；
  此时显存已被 9.1 GiB 权重占住，只剩碎片，`-ngl` 再高也无法解决。
- 模型本身没有损坏：ADR-008 已用 sha256 闭环验证 Q6_K 完整性；这里是容量问题，不是完整性问题。
- Q4_K_M 已下载并校验：`/home/cielo_1/models/gemma4-v2-Q4_K_M.gguf`，6.9 GiB，
  sha256 `0b9506cab36f7f818e34f9c0f5a3d6568d0b37100f3a3e1092e2eec3c4c96791`
  （GGUF v3，gemma4，48 层，12B，16 heads，sliding window 1024）。
- 省下的约 2.2 GiB 权重正好覆盖 KV cache 与 CUDA 上下文开销。

## 决策

1. `[models.main].gguf` 指向 `gemma4-v2-Q4_K_M.gguf`。
2. `[models.main].n_gpu_layers = 99`：Q4 稠密 48 层，先尝试全量 offload；
   显式值让实际 ngl 可通过 `ServerSupervisor.gpu_layers` 追溯，而不是静默交给 `--fit on`。
3. `n_ctx = 4096` 保持不变；`--parallel 1` 保持不变（避免多 slot 摊薄上下文）。
4. 继续遵守 ADR-008：换模型必须先过 sha256 门禁，大小相同不代表内容正确。
5. Q6_K 不再作为默认主脑；`llama31` profile 仍保留为应急回退（ADR-007）。

## 理由

- 12 GB 卡上 12B 模型的可行组合是 Q4 + 4096 ctx；Q6 + 4096 ctx 的 KV 连续分配必败。
- Q4_K_M 是质量/体积折中档，明显优于 Q2/Q3，同时给 KV cache 留出足够余量。
- 显式 `-ngl 99` 比 `--fit on` 更可观测：启动日志与 `gpu_layers` 能回答"实际跑了几层"。

## 代价与限制

- Q4 量化会带来一定质量下降；若后续评测显示不足，应换更小 ctx 或更高质量量化，
  而不是回到 Q6 + 4096 的必败组合。
- 显式 `-ngl 99` 在驱动/显存状态变化时可能首次失败，需依赖 ADR-034 的降档阶梯兜底。
- 模型文件从 9.1 GiB 降到 6.9 GiB，冷加载时间缩短，但这不是本决策的主要目标。

## 验证方式与结果（2026-10-30 18:11 CET 实际执行）

| # | 检查 | 结果 |
| --- | --- | --- |
| 1 | `sha256sum gemma4-v2-Q4_K_M.gguf` | `0b9506cab36f...c4c96791`，与记录一致 |
| 2 | `python3 -c "from agent.config import load; ..."` | `main` → Q4_K_M，`n_ctx=4096`，`n_gpu_layers=99` |
| 3 | `python3 -m compileall -q agent` | 通过，无报错 |
| 4 | `python3 -m agent --once "ping" --json` | `kind=ANSWER`，8 事件，33.47 s，`retries=0 failures=0` |
| 5 | `runs/server/main.log` | `=== ready: main -ngl 99 (attempt 1/10) ===`，无 OOM |
| 6 | llama.cpp 计时 | prompt eval 1982 tok/s，生成 41.80 tok/s |
| 7 | 进程回收 | 运行结束后无残留 `llama-server`，显存回到约 940 MiB |

> 注：本次 `ping` 的计划是 `workspace.echo → answer`，答案 `pong` 被 reviewer 判 `REVISE`
> （认为应逐字回显 `ping`）。这是评审策略对语义的严格要求，不是主脑加载/推理故障；
> 管线、ledger、report 均正常。
