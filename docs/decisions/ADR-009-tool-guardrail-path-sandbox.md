# ADR-009：工具护栏与路径沙箱（CrewAI 对齐的 GuardrailOutcome）

- **状态**：已采纳（2026-10-29）
- **关联代码**：`agent/core/policy.py:Guardrail`、`Guardrail.check_tool`、`GuardrailOutcome`、`agent/core/harness.py:Harness._execute_tool`、`agent/tools/workspace.py:ListDirTool`、`agent/tools/workspace.py:ReadFileTool`
- **关联 ADR**：ADR-001（ErrorInfo/错误码）、ADR-005（失败以数据表达）、ADR-006（TB-0 范围）、ADR-010（另一个 TB-1 决策）
- **上游文档**：`docs/pre_architecture.md` ADR-5（guardrail 对齐 CrewAI）、§3 TB-1 行、§6.1 工具目录

## 背景

TB-1 引入真正读文件系统的工具（`workspace.list_dir` / `workspace.read_file`）。模型给出的路径是**不可信输入**：
`../../etc/passwd`、绝对路径 `/home/...`、`.env`、`~/.ssh/id_rsa` 都可能被"顺手"读出来并写进 ledger。
需求同时要求 guardrail 与 CrewAI 保持一致（`success/value/error/retries`，默认 3 次重试语义）。

TB-0 只有 `workspace.echo`，没有外部资源，因此护栏可以推迟；TB-1 一旦接真实文件，护栏必须同步上线，
否则"先跑通再补安全"会变成"先把密钥写进日志再补"。

## 决策

1. `Guardrail` 是 core 内的纯校验器，**不执行 IO**：输入 `ToolCall + ToolSpec`，输出 `GuardrailOutcome{success, value, error, retries}`。
2. TB-1 只实现 `check_tool`（输入/输出护栏按 ADR-006 的节奏留给 TB-6），但类型与语义已与 CrewAI 对齐，后续加 `check_input/check_output` 不改调用方。
3. **路径沙箱**：凡 `args_schema` 声明了 `path` 属性的工具，一律：
   - 相对路径按 `workspace.root` 解析，`..` 由 `Path.resolve()` 折叠；
   - 解析后必须 `is_relative_to(root)`，否则拒绝（防目录穿越）；
   - 任意路径段命中 `deny_names`（默认 `.env`、`.git`、`id_rsa`、`secrets.toml`、`.ssh`）即拒绝；
   - 通过后返回**归一化后的绝对路径**，工具只接受归一化结果（避免"检查用相对、执行用绝对"的 TOCTOU 空子）。
4. 拒绝不是异常：返回 `success=False` + `ErrorInfo(code=DENIED_BY_GUARDRAIL, retryable=False)`，
   由 harness 转成 `TOOL_RESULT(ok=false)` 并照常入 ledger —— 与 ADR-005"错误也是数据"一致。
5. 拒绝**不消耗**工具重试预算（TB-6 的 3 次重试针对可重试错误；越权是策略性失败，重试没有意义）。

## 理由

- 护栏放在 core、不放工具内部：工具实现可以替换（本地/远程/HTTP），而"允许读哪里"必须与实现无关。
- 归一化后再执行，是因为"先校验字符串、再让工具自己拼路径"是最常见的绕过方式。
- 拒绝返回数据而不是抛异常：ledger 里能留下"模型试图读 `.env`"的证据，这正是监督与反思需要的素材。

## 代价

- 每个带 `path` 的工具都要经过一次 `resolve()`（可忽略）。
- 沙箱只覆盖带 `path` 参数的工具；未来出现 `paths[]`、`cwd`、`glob` 等参数必须扩展规则，否则会有缺口（记入 TB-6 待办）。
- 默认拒绝列表是静态的，用户目录下若有同名合法文件需要改配置。

## 验证方式

- 单测式断言：`../../etc/passwd`、`/etc/passwd`、`.env`、`~/.ssh/id_rsa` 全部 `DENIED_BY_GUARDRAIL`；
  `docs/pre_architecture.md` 通过且返回绝对路径。
- 实跑：让主脑读一个越权路径，ledger 中应出现 `TOOL_RESULT ok=false code=DENIED_BY_GUARDRAIL`。
