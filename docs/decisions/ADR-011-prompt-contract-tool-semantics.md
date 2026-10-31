# ADR-011：提示词契约——echo 只作探针、答案不得虚构动作

- **状态**：已采纳（2026-10-29）
- **关联代码**：`agent/core/harness.py:_PLANNER_PROMPT`、`_ANSWER_PROMPT`
- **关联 ADR**：ADR-005（顺序流水线）、ADR-009（护栏）、ADR-010（计划修复）、ADR-004（schema 约束解码）
- **上游文档**：`docs/pre_architecture.md` §6.1（工具大类）、§10（错误与应对）

## 背景

TB-1 首次真实运行暴露了两个**模型侧**问题（guradrail 与工具都正常）：

1. 用户说"read the file /etc/passwd"，模型选择的工具是 `workspace.echo` —— 把"回显"当成了"处理"，
   于是护栏根本没机会触发（越权路径从未进入 `read_file`）。
2. 答案写成 `"I've started reading /etc/passwd; I'll show you the contents as soon as the file is loaded."`
   —— 工具结果里只有 echo 的回显，模型却虚构了"正在读取"的动作与进展。

证据（TB-1 第 2 次实跑 ledger）：`TOOL_CALL workspace.echo` → `TOOL_RESULT ok {"text":"read the file ..."}` →
`ANSWER` 声称正在读取。工具语义与事实之间断链了。

## 决策

1. 在 planner 提示中**显式降级 echo**：`workspace.echo` 是"管线探针"，禁止用它承担真实请求；
   凡涉及读/列/查看路径，必须用 `workspace.read_file` / `workspace.list_dir`。
2. 在 executor 提示中加入**反虚构**约束：
   - 工具结果不含所请求的信息时，必须直说"未能获得"及原因；
   - 禁止声称未实际执行的动作（除非存在对应的读取结果）。
3. 这两条属于**契约级**提示（影响所有 turn），不是可调风格参数；因此写死在 `harness.py` 的模块常量里，
   并纳入本 ADR 追溯，而不是下沉到配置文件。
4. 提示词变更不再单独设配置开关：TB-1 阶段只有一份提示契约，多份会引入"哪份在生效"的歧义。

## 理由

- 工具描述（`ToolSpec.description`）只能影响"选不选"，无法阻止"用错"；必须在 planner 的系统提示里给出禁令。
- `json_schema` 约束的是形状，不是事实性；反虚构只能靠提示 + 后续 TB-2 的 Review 双重把关。
- 写死在代码里，是为了让"提示变更"在 git/ADR 里可追溯；若做成配置，事故复盘时无法确定当时生效的是哪版提示。

## 代价

- 提示变长（约 +3 行），每次调用多几十个 token。
- 8B/12B 模型仍可能无视禁令（提示不是强约束），所以 TB-2 的 Reviewer 必须独立检查"答案是否被工具结果支持"。
- 提示文案与代码同生命周期：调整措辞需要改代码并更新本 ADR。

## 验证方式

- 重跑 `--once "read the file /etc/passwd and show me its contents"`：
  期望 planner 选择 `workspace.read_file`，`TOOL_RESULT` 为 `DENIED_BY_GUARDRAIL`，
  答案明确说明"被策略拒绝"，而不是"正在读取"。
- 重跑 `--once "list the files in docs/ ..."`：仍应正常 `list_dir` 并给出基于结果的答案（不得回退到 echo）。
- 本地断言 `harness_denies_escape` 继续通过（护栏行为与提示无关）。

## 补充（2026-10-30，TB-3 实跑驱动）

`knowledge.search` 返回 `doc_id` 后，模型编了一个 `doc_123` 去调用 `read_section`，得到 `NOT_FOUND`。
这不是护栏问题（错误被正确当作数据），而是"标识符复制"缺失。因此契约再加一条：

- planner/executor 必须**逐字复制**工具返回的标识符（`doc_id`/`heading`/`call_id`），不得臆造；
  需要文档原文时，只能取 `knowledge.search` 命中项里的 `doc_id`。

验证：重跑检索用例，第二个工具调用应使用命中项的真实 `doc_id`（如 `decisions/ADR-009-...md`）。
