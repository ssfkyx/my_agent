# ADR-006：TB-0 切片范围（只做"能跑通的一段"）

- **状态**：已采纳（2026-10-29）
- **关联代码**：`agent/core/protocol.py:MessageKind`（仅 6 种）、`agent/core/harness.py:Harness`、`agent/tools/workspace.py:EchoTool`、`agent/__main__.py:main`
- **关联 ADR**：ADR-001、ADR-002、ADR-004、ADR-005
- **上游文档**：`docs/pre_architecture.md` §3（TB-0 行）、§8（目录结构）

## 背景

`docs/pre_architecture.md` 把交付切成 TB-0…TB-7。若一次性实现评审、记忆、RAG、媒体、升级，首次运行会同时暴露十几种失败原因，无法判断架构是否成立。Tracer bullet 的全部意义是：先钉死一条最细的、端到端贯通的路。

## 决策

TB-0 只实现以下内容，其余一律留给后续切片：

| 维度 | TB-0 有 | TB-0 没有（后续切片） |
| --- | --- | --- |
| 信封 kind | `USER_INPUT` `PLAN` `TOOL_CALL` `TOOL_RESULT` `ANSWER` `ERROR`（TB-1 追加 `RETRY`，见 ADR-010） | `INTENT` `REVIEW` `RUN_REPORT` `REFLECTION` `CONSENT_REQUEST` `ROLE_SWITCH` |
| 工具 | `workspace.echo`（管线连通性探针） | `workspace.read_file/list_dir`（TB-1，已实现）、`knowledge.*`（TB-3）、`delegate.*`（TB-2） |
| 模型 | 只有主脑 `gemma4-v2` | 次脑 LFM2.5（TB-2）、ASR/VL（TB-5） |
| 控制 | `max_steps` 截断 + 异常转 `ERROR`（TB-1 追加工具护栏与 1 次计划修复，见 ADR-009/010） | 重试预算 3 次、ProgressWatch、Escalator（TB-6） |
| 界面 | `--once` 单轮 + ANSI 行式监督输出 | curses 交互、Web（`ui/web/`） |
| 存储 | ledger 落盘 `runs/<turn_id>.jsonl` | SQLite 向量库、reflection.jsonl |

同时对**今后不许倒退**的点做出约束：信封字段名、`ToolCall/ToolResult` 形状、`ServerSupervisor` 单实例、`Plan.why` 必填。

## 理由

- kind 枚举只列已实现的成员：新增 kind 必须改 codec，这是一次有意识的检查点，而不是让枚举先长满再说。
- `workspace.echo` 不是玩具：它是 `--once ping` 的确定性探针，用来把"模型 → 计划 → 工具 → 回执 → 答案"整条链路与模型质量解耦。若 TB-0 用 `read_file`，验收就会同时依赖模型选对工具。
- 不做 curses 交互，因为 TB-0 的验收是机器可判定的单次运行；交互层在 TB-2 有评审流之后才值得写。

## 代价

- TB-0 完成后用户还不能连续对话，必须等 TB-1/TB-2。
- `MessageKind` 会随切片增长，历史 ledger 的兼容由"只增不改"策略保证（旧文件仍可解析）。
- `EchoTool` 在 TB-1 后仍保留，作为排障工具常驻（不是死代码）。

## 验证方式

- `python -m agent --once "ping" --json` 输出 `kind=ANSWER` 的合法信封；
- `runs/<turn_id>.jsonl` 事件数 ≥ 5 且以 `ANSWER` 结尾；
- `python -X importtime` 依赖集合中无第三方包。
