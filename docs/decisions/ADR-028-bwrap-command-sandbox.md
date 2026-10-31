# ADR-028：workspace.run_command 的 bwrap 沙箱

- **状态**：已采纳（2026-10-30）
- **实现状态**：已落地（2026-10-30）
- **关联代码**：`agent/tools/workspace.py:RunCommandTool._wrap`、
  `agent/config.py:WorkspaceLimits.command_sandbox`、`agent/app.py`（注入 write_root/sandbox）
- **关联 ADR**：ADR-026（workspace 写/执行策略）、ADR-009（路径沙箱）、ADR-003（stdlib only）
- **上游文档**：`docs/pre_architecture.md` §10（越权命令）、§12 下一步

## 背景

ADR-026 的 run_command 只有三层软防线：

1. argv 前缀白名单；
2. 拒绝绝对路径/`~`/`..` 参数；
3. 固定 cwd、最小环境、超时与输出上限。

这些不能阻止白名单命令本身越界，例如 `ls /etc` 被参数检查挡住，但 `python3 -c` 一旦进入白名单
就可以读写宿主文件系统、访问网络。宿主已安装 `bwrap`（bubblewrap），因此增加一层 OS 级隔离。

## 决策

1. **配置开关 `[workspace].command_sandbox`**：
   - `"bwrap"`：强制沙箱；找不到 `bwrap` 时构造工具失败（fail closed）；
   - `"auto"`（默认）：找到 `bwrap` 就用，否则退回直接执行；
   - `"none"`：显式关闭 OS 沙箱，仅保留 ADR-026 的软防线。
2. **沙箱内文件系统**：
   - `/usr`、`/bin`、`/sbin`、`/lib`、`/lib64`、`/etc` 只读绑定；
   - `workspace.root` 只读绑定；
   - `[workspace].write_root`（默认 `artifacts/`）再次以读写绑定覆盖；
   - `/tmp` 使用 tmpfs；先挂 tmpfs 再挂 workspace，避免 workspace 位于 `/tmp` 下时被遮住；
   - `/proc`、`/dev` 提供基本运行环境。
3. **网络与进程隔离**：`--unshare-all`（含 network namespace）、`--die-with-parent`；
   命令无法联网，父进程退出时子进程被杀死。
4. **环境变量**：`--clearenv` 后用 `--setenv` 注入最小环境（PATH/HOME/LANG/LC_ALL/
   PYTHONPATH/PYTHONIOENCODING/GIT_OPTIONAL_LOCKS），不继承宿主密钥。
5. **软防线继续保留**：白名单、参数路径检查、超时、输出上限在沙箱之前执行；沙箱是
   defense-in-depth，不是替代品。
6. **可观测**：`workspace.run_command` 的 `data.sandbox` 返回 `"bwrap"` 或 `"none"`，
   ledger/report 能看出本条命令是否在沙箱内。

## 理由

- workspace 根只读、只有 `write_root` 可写，与 ADR-026 的写边界一致；命令不能借执行能力
  改写源码/配置/ledger。
- 无网络命名空间能阻止“读本地文件后外发”的简单路径；比仅靠参数检查可靠。
- `auto` 让开发机/CI 在无 bwrap 时仍可运行；需要强保证时显式设 `"bwrap"`。
- 不引入第三方 Python 包，只依赖宿主可执行文件。

## 代价与限制

- 仅支持 Linux + bwrap；Windows/macOS 只能用 `none` 或后续换 adapter。
- 白名单命令若需要联网或写 `artifacts/` 之外的路径，会失败；这是有意的安全取舍。
- `/etc` 只读绑定仍让命令能读系统配置；沙箱不是“秘密隔离”边界。
- `bwrap` 本身若有漏洞或宿主内核配置异常，隔离强度下降；本 ADR 不承诺容器级安全。
- `auto` 在 bwrap 缺失时会静默退回 `none`；需要强制保证时应配置 `"bwrap"`。

## 验证方式

- 单测/断言（临时 workspace，`artifacts/` 可写）：
  - `python3 --version` 成功，`data.sandbox == "bwrap"`；
  - `ls artifacts` 成功；
  - 写 `outside.txt` 返回码非 0，错误为 `Read-only file system`，文件未创建；
  - 写 `artifacts/inside.txt` 成功；
  - `socket.create_connection(("1.1.1.1", 53))` 返回码非 0，错误为 `Network is unreachable`。
- 配置：默认 `[workspace].command_sandbox = "auto"`；`bwrap` 不存在时 `"bwrap"` 模式在
  `RunCommandTool` 构造期报错。
- 端口约束：`config.load` 拒绝模型/Web 使用 8080、5117（宿主已占用）。
