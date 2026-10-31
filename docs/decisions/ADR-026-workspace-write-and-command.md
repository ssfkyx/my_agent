# ADR-026：工作区写文件与白名单命令执行

- **状态**：已采纳（2026-10-30）
- **实现状态**：已落地（2026-10-30）
- **关联代码**：`agent/config.py:WorkspaceLimits`、`agent/core/policy.py:GuardrailPolicy.write_root`、
  `Guardrail.check_tool`、`agent/tools/workspace.py:WriteFileTool`、`RunCommandTool`、`agent/app.py`
- **关联 ADR**：ADR-009（路径沙箱）、ADR-005（失败作为数据）、ADR-003（stdlib only）、ADR-019（输出上界）
- **上游文档**：`docs/pre_architecture.md` §6.1（workspace 大类）、§10（写/执行受 guardrail）

## 背景

工具目录一直计划 `workspace.write_file` / `workspace.run_command`，但此前只有只读工具。
直接开放写/执行会突破现有的路径沙箱假设：

- 写文件可能覆盖源码、配置或审计记录；
- 命令执行可以通过绝对路径/`..` 读取工作区外内容，甚至启动任意进程；
- 继承完整环境变量会把密钥暴露给子进程。

因此不能只给 `ToolSpec.side_effect=True` 就结束，必须同时定义写边界与执行边界。

## 决策

1. **写路径限制在 `write_root`**：`[workspace].write_root`（默认 `artifacts/`）必须是
   `workspace.root` 的子目录。Guardrail 对所有 `side_effect=True` 且 `category="workspace"`
   的 path 参数做二次检查；路径不在 `write_root` 内 → `DENIED_BY_GUARDRAIL`。
   `write_root` 为空字符串 → 写工具整体禁用。`knowledge.index` 等非 workspace 写工具不受此限制。
2. **写文件默认不覆盖**：`workspace.write_file` 只写 UTF-8 文本，单次上限
   `max_write_bytes`（默认 64 KiB）；目标已存在时必须显式 `overwrite=true`。
   父目录只在 `write_root` 内创建。
3. **命令用 argv 数组，不用 shell**：`workspace.run_command` 的 schema 是
   `argv: string[]`；`subprocess.run(argv, shell=False)`；`cwd` 固定为 workspace root。
4. **命令白名单是 argv 前缀**：配置 `[workspace].command_allowlist` 是嵌套数组；
   例如 `["git","status"]` 允许 `git status --short`，但不允许 `git push`。
   空数组表示禁用命令执行。默认只读：`ls`、`wc`、`git status`、`git diff`、`python3 --version`。
5. **参数路径检查**：拒绝绝对路径、`~`、包含 `..` 的参数，以及解析后逃出 workspace 的相对路径；
   `--key=/abs/path` 也会检查等号后的值。这是启发式防线，不是 OS 级沙箱。
6. **最小环境**：子进程只拿到 `PATH/HOME/LANG/LC_ALL/PYTHONPATH/PYTHONIOENCODING`，
   `HOME` 指向 workspace root，`GIT_OPTIONAL_LOCKS=0`；不继承 API key 等宿主环境。
7. **输出与超时**：`command_timeout_s`（默认 15 s）与 `max_command_output_bytes`
   （默认 64 KiB/路）限制资源；超时返回 `TIMEOUT`（不重试），非零退出码作为数据
   （`ok=True` + `returncode`），越权/未命中白名单返回 `DENIED_BY_GUARDRAIL`（不重试）。
8. **副作用可见**：两个工具都声明 `side_effect=True`，计划提示与报告能看出它们是写/执行步骤。

## 理由

- 写边界放在 Guardrail 而不是工具内部，保证以后新增 workspace 写工具自动继承同一策略。
- 默认只写 `artifacts/`，让“产出物”与源码/配置/ledger 分离，降低误覆盖风险。
- argv 前缀白名单比“命令名字符串 + shlex”可审计：没有 shell 元字符、没有管道/重定向。
- 超时/输出上界与 ADR-019 一致，避免一条命令拖垮 turn 或撑爆上下文。

## 代价与残余风险

- 白名单命令本身仍可能产生副作用（例如 `git` 的 external diff/文本转换）；
  对不可信仓库应进一步收紧甚至禁用 run_command。
- 参数路径检查是启发式的；ADR-028 已增加可选 bwrap 沙箱（workspace 只读、仅 write_root 可写、无网络），
  `command_sandbox=none` 时仍只剩这层启发式防线。
- 写文件不是原子替换；进程在写入中途崩溃可能留下半文件。
- `write_root` 默认 `artifacts/` 会创建该目录；用户可改为其他子目录或禁用。

## 验证方式

- 断言：
  - Guardrail 允许 `artifacts/...`，拒绝 `agent/...`、`../...`，`write_root=None` 时拒绝所有写；
  - `write_file` 创建/拒绝覆盖/允许 overwrite/超长拒绝；
  - `run_command` 允许白名单，拒绝 `rm`、绝对路径、`..`，超时返回 `TIMEOUT`。
- 实跑：
  - `t-20261030-145412-9825be`：成功写入 `artifacts/hello.txt`（16 B）并执行
    `python3 --version`，`retries=0 failures=0 review=PASS`；
  - `t-20261030-145530-c60cde`：写 `agent/app.py` 被 `DENIED_BY_GUARDRAIL`（outside write_root），
    `rm -rf artifacts` 被 `DENIED_BY_GUARDRAIL`（not in allow-list），答案如实说明，`failures=2`。
