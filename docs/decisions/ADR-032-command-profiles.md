# ADR-032：模型自选命令 profile

- **状态**：已采纳（2026-10-30）
- **实现状态**：已落地（2026-10-30）
- **关联代码**：`agent/config.py:WorkspaceLimits.command_profiles`、
  `agent/tools/workspace.py:RunCommandTool`、`agent/config/defaults.toml`
- **关联 ADR**：ADR-026（写/执行）、ADR-028（bwrap 沙箱）
- **上游文档**：`docs/pre_architecture.md` §6.1（workspace 工具）、§10（越权命令）

## 背景

ADR-026/028 的 `workspace.run_command` 只有一组全局 `command_allowlist`。模型无法在不
改配置的情况下从“只读检查”切换到“git 检查”或“python 校验”；用户要求“任务 profile
选择权归模型，达成模型自选武器的效果”。

## 决策

1. **配置结构**：
   - `[workspace].command_allowlist` 是隐式的 `default` profile；
   - `[[workspace.command_profiles]]` 追加命名 profile：
     ```toml
     [[workspace.command_profiles]]
     name = "git"
     allowlist = [["git", "status"], ["git", "log"]]
     ```
   - profile 名非空、唯一、不能叫 `default`；每个 allowlist 仍是 argv 前缀数组。
2. **工具参数**：`workspace.run_command` 新增可选 `profile: string`；省略时用 `default`。
   模型可自行选择 `profile="git"` / `"python"` 等，即“自选武器”。
3. **选择规则**：
   - 未知 profile → `BAD_ARGS`，hint 列出可用 profile；
   - 选定 profile 后，argv 必须匹配该 profile 的前缀；否则 `DENIED_BY_GUARDRAIL`；
   - 路径检查、超时、输出上限、bwrap 沙箱对所有 profile 一视同仁。
4. **可观测**：`ToolResult.data.profile` 返回实际使用的 profile；ledger/report 可追溯。
5. **默认 profile 保持兼容**：不传 `profile` 的旧调用/测试行为不变；默认 `command_allowlist`
   仍是只读命令（ls/wc/git status/diff/python3 --version）。
6. **示例 profile**：默认配置提供 `git`（status/diff/log/show）与 `python`
   （--version/compileall/unittest）；用户可增删，配置是唯一的权限上限。

## 理由

- 模型自选 profile 比“改全局白名单”灵活，同时每个 profile 仍由用户预先定义，模型不能
  创造新命令权限。
- profile 放在工具参数而不是多套工具，避免工具目录膨胀；schema 只增加一个可选字符串。
- 保留 `default` 隐式 profile，旧脚本/模型提示无需改动。

## 代价与限制

- profile 列表是模型可见的“武器库”；配置过多会扩大攻击面，应保持最小集合。
- 模型可能选错 profile；错误会以 `BAD_ARGS`/`DENIED_BY_GUARDRAIL` 返回，不重试。
- profile 不能按目录/项目自动切换（那需要额外的 cwd/仓库识别策略）；当前是配置级。
- bwrap 下 workspace 只读、仅 `write_root` 可写；`python -m compileall` 等会写
  `__pycache__` 的命令可能因只读失败，这是沙箱的有意取舍。

## 验证方式

- 配置：`load()` 返回 `command_profiles=[('git', ...), ('python', ...)]`；
  重复名/`default` 名/空前缀在 `WorkspaceLimits` 校验失败。
- 工具：
  - 不传 profile 的 `ls artifacts` 成功，`data.profile="default"`；
  - `profile="python"` + `python3 --version` 成功；
  - `profile="git"` + `git status` 允许（无仓库时 returncode 非 0，但工具 ok）；
  - `profile="git"` + `rm -rf` → `DENIED_BY_GUARDRAIL`；
  - `profile="nope"` → `BAD_ARGS`，hint 列出可用 profile。
