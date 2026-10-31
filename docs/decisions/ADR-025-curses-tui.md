# ADR-025：curses 单窗口 TUI

- **状态**：已采纳（2026-10-30）
- **实现状态**：已落地（2026-10-30）
- **关联代码**：`agent/ui/tui.py:TuiUI`、`agent/ports.py:InteractiveUiPort`、
  `agent/app.py:App.run_session`、`agent/__main__.py:--tui`
- **关联 ADR**：ADR-003（stdlib curses）、ADR-006（UI 只消费 Envelope）、
  ADR-021（同意询问）、ADR-023（ROLE_SWITCH 渲染）
- **上游文档**：`docs/pre_architecture.md` §9（四区布局与配色 token）、R3（先 TUI 后 Web）

## 背景

TB-0～TB-7 的 `ConsoleUI` 是行式输出，适合 `--once`/脚本，但 §9 的“阶段一（现在）”
要求单窗口 TUI：计划、监督流、对话/答案、评审/状态四区。核心流水线已稳定，
可以在不修改 core 的前提下增加第二个 `UiPort` 实现。

## 决策

1. **TUI 是第二消费者**：`TuiUI` 只实现 `UiPort` + `InteractiveUiPort`，只消费
   `Envelope` 与 `Presenter.project` 的结果；不 import Harness/Tool/模型适配器。
2. **多轮循环放在 App**：新增 `App.run_session()`，由它读取用户输入并调用 `run_once`；
   TUI 只负责 `read_user_input()` / `render()` / `ask_consent()` / `close()`。
3. **CLI 显式选择**：`--once TEXT` 保持单轮；`--tui` 进入交互；二者互斥且至少在 TTY 下运行。
   `--json` 只用于单轮脚本，不能与 `--tui` 组合。
4. **四区布局**：上区左侧 PLAN、右侧 SUPERVISION；中区 CONVERSATION / ANSWER；
   底部 REVIEW/REPORT/LEDGER 与输入行。终端过小（< 10 行或 < 50 列）降级为
   “最近事件 + 输入行”，不抛异常。
5. **事件驱动重绘**：`render` 按 Envelope 更新快照；监督区保留最近 120 条；计划步骤用
   `step_id` 跟踪 `pending/running/ok/fail`；`ROLE_SWITCH` 更新 header 的 role。
6. **同意提示就地弹出**：`ask_consent` 在对话区画 CONSENT 面板（问题 + 脱敏预览 + [y/N]），
   读 `y/yes` 才返回 True；`allow/deny` 模式不弹窗，与 CLI 语义一致。
7. **颜色近似而非真彩**：curses 用基础 8 色映射 §9 token；不引入第三方库，不依赖
   terminal truecolor 能力。无颜色终端静默退化为单色。
8. **单线程同步**：TUI 不做动画/后台刷新；模型调用期间只显示最后一条事件，符合
   “不做动画”的约定，也避免 curses 跨线程刷新。

## 理由

- TUI 作为第二个 UiPort 可以完全复用 `Presenter` 与所有 ledger 语义，core 零改动。
- 多轮循环属于编排（App），不放进 UI，保持“UI 不调用 core”的边界。
- 显式 `--tui` 让脚本/CI 继续使用稳定的 `--once --json`，不改变既有回归命令。
- 失败降级为可读的监督行/小终端布局，而不是让终端留在 raw mode。

## 代价

- 同步 UI：模型调用期间无法滚动/取消；取消要靠 Ctrl-C 结束进程。
- 监督区只保留最近 120 条，完整历史仍在 ledger JSONL。
- 同意预览在非常长的 payload 下会被面板高度裁切；完整内容仍进 ledger。
- 8 色近似与 §9 的十六进制配色不完全一致；Web 阶段可复用 token 名重新映射。

## 验证方式

- PTY（`script`）冒烟：`--tui --consent deny` 启动后输入 `/quit`，退出码 0，终端恢复。
- 完整一轮：输入 `ping`，TUI 渲染 `PLAN → TOOL_CALL → TOOL_RESULT → REVIEW → ANSWER →
  RUN_REPORT`，生成 `runs/t-20261030-132703-4c6c89.*`，`retries=0 failures=0`。
- 输入/同意提示：PTY 下 `read_user_input` 返回 `hello`，`ask_consent` 收到 `y` 返回 True。
- 非 TTY：`python -m agent --tui < /dev/null` 返回环境错误 3，不尝试初始化 curses。
