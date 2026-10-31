# ADR-035：token 预算统一为 8192

- **状态**：已采纳（2026-10-30）
- **实现状态**：已落地并实跑验证（2026-10-30）
- **关联代码**：`agent/config/defaults.toml`（`[harness].max_tokens` /
  `review_max_tokens`、`[models.main].n_ctx`）、`agent/core/harness.py:HarnessPolicy`、
  `agent/core/learning.py:Reviewer`、`agent/core/reflection.py:Reflector`
- **关联 ADR**：ADR-012（主脑 token 预算）、ADR-019（评审预算）、ADR-034（ngl 降档）
- **上游文档**：`docs/pre_architecture.md` §5.3、§11.1、§12

## 背景

T13 用完整 Terminal-Bench 指令时出现 `PLAN_INVALID`：

```
planning failed: model content is not JSON: {
  "goal": "Write a Bash script at artifacts/...
```

llama.cpp 日志给出了决定性证据：

| 指标 | 实测 |
| --- | --- |
| planner prompt | 3602–3690 tokens |
| 总上下文 | 4096 tokens |
| 实际生成 | 410–433 tokens 后停止 |
| 日志标记 | `n_tokens = 4095, truncated = 1` |

也就是说，`max_tokens = 1024` 并不是当时的真实瓶颈；**prompt + output 撞上了
`n_ctx = 4096`**。只提高 `max_tokens`（例如 10000）不会生效，因为剩余输出空间只有
约 430 tokens。

## 决策

1. `[models.main].n_ctx` 从 4096 提升到 **8192**。
2. `[harness].max_tokens` 从 1024 提升到 **8192**（planner / answer 共用）。
3. `[harness].review_max_tokens` 从 1536 提升到 **8192**（reviewer / reflection 共用）。
4. 保留 ADR-019 的 schema 边界（`issues`/`suggestions` 各最多 3 条、每条最多 240 字符）：
   预算放大不等于允许评审无限写长。
5. 不为 10000 输出 token 把 `n_ctx` 再推到 16384：12 GB 卡上 KV cache 约 4 倍，
   风险明显高于收益；8192 已足够覆盖当前计划的 prompt + 输出。

## 理由

- 统一为 8192 让配置语义简单：主脑上下文、主脑输出、评审输出上限一致，便于排查。
- 8192 的 KV cache 实测峰值约 9.1 GiB，12282 MiB 卡仍有约 3.1 GiB 余量。
- 输出上限只是“允许更长”，不会预分配显存；正常短计划仍只生成 200–400 tokens，
  延迟不会因为上限变大而线性增加。
- 保留 review 的 schema 上界，避免“预算变大 → 评审写更长 → 反而更容易截断”的退化。

## 代价与限制

- KV cache 从 4096 翻倍到 8192，峰值显存约 9.1 GiB；若宿主同时占用显存，
  `-ngl 99` 可能失败并触发 ADR-034 降档。
- `max_tokens = 8192` 在 `prompt ≈ 3600` 时仍不可完全达；真实输出上限约 4500 tokens。
  如果未来需要真正 8192 输出，必须把 `n_ctx` 提到 12288–16384，届时很可能 OOM。
- 最坏情况单次生成 8192 tokens 约需 3 分钟（约 42 tok/s）；这是上限，不是常态。
- 统一预算没有解决“模型答非所问/路径拼写错误”的质量问题；T13 第二次实跑仍出现
  answer 文本与实际工具结果不一致，需要后续提示词或评审策略改进。

## 验证方式与结果

**静态**

- `load()`：`n_ctx=8192`、`max_tokens=8192`、`review_max_tokens=8192`。
- `compileall` 通过。

**实跑（2026-10-30 19:46 CET）**

| # | 用例 | 结果 |
| --- | --- | --- |
| 1 | 完整 Terminal-Bench 指令（之前 `PLAN_INVALID`） | `t-20261030-194629-b918e0`：计划成功（3 步），27.52 s，`review=PASS`，`failures=0` |
| 2 | 同一轮的显存 | 峰值 **9136 MiB** / 12282 MiB |
| 3 | `--once "ping" --json` | `t-20261030-194707-7cb4b1`：24.05 s，`review=PASS`，`models={'main': 99, 'sub': 99}` |
| 4 | ping 轮显存 | 峰值 **9133 MiB**，无 OOM、无降档 |
| 5 | 进程清理 | 两轮结束后均无残留 `llama-server`，端口释放 |

> 备注：完整指令轮的最终 answer 文本写成“I'll read the existing solution.sh…”，
> 与已成功的 `write_file` 结果不一致；review 仍判 PASS。这是模型质量/评审覆盖问题，
> 与本 ADR 的 token 预算无关，已记录在 WORKLOG。
