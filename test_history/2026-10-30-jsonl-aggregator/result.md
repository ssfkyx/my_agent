# Result — 2026-10-30

## 评审门禁前（pre_gate）

默认 `max_steps=4`，共跑 3 次，**0/3 通过**。

| 运行 | 配置 | 结果 |
| --- | --- | --- |
| `t-20261030-203118-18224f` | 默认 4 步 | 使用不存在的 `count` 字段；烟测失败 |
| `t-20261030-203228-0da24f` | 临时 `max_steps=7` | 输出自定义结构且 `json.dump` 括号不匹配；烟测失败 |
| `t-20261030-203343-620e83` | 继续修复轮 | f-string `SyntaxError`；answer 声称 “All tests passed”，但没有运行烟测的 TOOL_CALL |

三次评审均为 `REVISE 0.0`，说明次脑看见了问题，但当时评审没有控制权。

原始 ledger/report 在 `agent_runs/pre_gate/`。

## 评审门禁后（post_gate）

实现内容：

- `REVISE/FAIL` 触发 planner 重新规划，反馈包含 review issues/suggestions
- 证据校验：声称测试通过但没有 `returncode=0` 的测试结果时，强制 REVISE
- 动态步数：基础 4 步，每次打回追加 4 步，总上限由 `max_review_repairs` 决定
- 工具失败详情也会进入重做反馈

### 运行记录

| 运行 | 配置 | 结果 |
| --- | --- | --- |
| `t-20261030-204736-5b7959` | 默认，2 次打回 | 3 个 PLAN、2 次 RETRY、12 步；路径写错到 `artifacts/terminal_bench/...`，最终 `FAIL` |
| `t-20261030-205441-35cb13` | 默认，2 次打回 | 写入正确目录，但 schema/逻辑仍错；`FAIL` |
| `t-20261030-210137-5cbc6c` | 默认，2 次打回 | 数值基本正确，但输出 key 是 `top_users/top_tags`；`REVISE 0.5`，未通过 |
| `t-20261030-210820-0f52fd` | `max_review_repairs=3`，显式 schema 提示 | **PASS 1.00，retries=0，120.04 s**；`aggregates.json` 与官方 expected 完全一致 |
| `t-20261030-211034-820a7d` | 默认，2 次打回，同提示 | 429.74 s，2 次 RETRY，最终仍 `FAIL`；`len(items)` 误用且没有写 `aggregates.json` |

### 结论

- 评审门禁、动态步数、重做原因回传都已实际触发；不是“只记录不动作”。
- 但 Q4 的代码生成稳定性仍然低：
  - 5 次 post-gate 运行中只有 1 次达到官方 exact match；
  - 通过的一次是 3 次打回预算 + 显式 schema 提示，且实际 `retries=0`（一次成功）；
  - 同样提示下默认 2 次打回仍然失败，说明方差主要来自模型，而不是门禁逻辑。
- 证据校验在单测中覆盖；本次 live run 的失败大多没有“测试通过”声明，因此由 reviewer 的 `FAIL/REVISE` 门禁接管。
- 下一步若要提高稳定通过率，优先考虑：
  1. 对代码任务提供更强的结构化输出校验；
  2. 写文件后强制“最后一次验证必须在最后一次写入之后”；
  3. 对 `len(items)` 这类 schema/类型歧义，在 task 中给出更明确的字段类型。

原始 ledger/report/失败代码在 `agent_runs/post_gate/`；通过运行的脚本与输出在 `pass_3repairs/`。

## 参考解

`reference_aggregates.json` 与官方 expected 完全一致，任务本身可解。
