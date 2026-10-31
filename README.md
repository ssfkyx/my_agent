# Local Agent Harness

> 这是一个可人工演进、主脑可提改进方向、次脑可选把关、可选多模态、带本地 RAG 和完整审计的单机 agent harness；

**基于本地 `llama.cpp` .模型需要在本机提供 GGUF。参考必选模型和可选模型。

## 特性

- `PLAN → ACT → REVIEW → REFLECTION → ANSWER` 顺序编排
- JSONL ledger、RunReport JSON/Markdown、每轮模型与 ngl 记录
- 次脑可选评审、打回重做、证据校验、动态步数
- 主脑可提交结构化 harness 改进提案，提案只归档、不自动应用
- 本地 Markdown RAG：section 父块、向量 + FTS5/BM25 + RRF
- CLI、curses TUI、Web UI/SSE、多会话、Markdown 渲染
- 图片/音频附件上传；ASR/VL 可选
- workspace 路径沙箱、命令白名单、bwrap 沙箱
- 单常驻模型、ngl 降档重试、SIGTERM 进程树清理
- Windows / Linux / macOS 双击启动器

## 必选模型

```text
main   主脑
embed  RAG 嵌入
```

## 可选模型

```text
sub       次脑 / review / reflection / delegate
asr       语音转写（需要 mmproj）
vl        图片描述（需要 mmproj）
llama31   预留，不参与当前编排
```

不配置可选模型时，agent 仍可完成纯文本任务。

## 环境

- Python 3.11+（使用 stdlib `tomllib`）
- 可执行的 `llama-server`（llama.cpp）
- 本机 GGUF 模型文件

## 双击启动

仓库根目录提供启动器：

```text
Windows   start.bat
Linux     start.sh
macOS     start.command
```

双击后默认执行：

```bash
python3 start.py --web
```

并自动打开 Web UI。启动器支持透传 agent 参数：

```bash
./start.sh \
  --model main=/path/to/main.gguf \
  --model embed=/path/to/embed.gguf \
  --disable sub --disable asr --disable vl
```

如果只是启动 Web 但不想自动打开浏览器：

```bash
./start.sh --no-browser
```

Windows 上如果 `py`/`python` 不在 PATH，`start.bat` 会暂停并保留错误信息。

## 快速启动

最小配置只需要主脑和嵌入模型：

```bash
python3 -m agent \
  --model main=/path/to/main.gguf \
  --model embed=/path/to/embed.gguf \
  --disable sub --disable asr --disable vl \
  --once "介绍一下这个项目"
```

启动 Web：

```bash
python3 -m agent \
  --model main=/path/to/main.gguf \
  --model embed=/path/to/embed.gguf \
  --disable sub --disable asr --disable vl \
  --web
```

启动 TUI：

```bash
python3 -m agent \
  --model main=/path/to/main.gguf \
  --model embed=/path/to/embed.gguf \
  --tui
```

## 启用可选模型

```bash
python3 -m agent \
  --model main=/path/to/main.gguf \
  --model sub=/path/to/sub.gguf \
  --model embed=/path/to/embed.gguf \
  --model asr=/path/to/asr.gguf \
  --model vl=/path/to/vl.gguf \
  --once "..."
```

ASR/VL 的 `mmproj` 仍由 `[models.<alias>].mmproj` 配置提供。

- 没有 `sub`：Reviewer/Reflector 不启用，delegate 工具不注册，报告显示 `review=n/a`
- 没有 ASR：音频上传和 `perception.transcribe` 关闭
- 没有 VL：图片上传和 `perception.describe_image` 关闭

## 查看模型清单

```bash
python3 -m agent --list-models
python3 -m agent --list-models --json
```

`--list-models` 只读取配置和探测文件是否存在，不启动任何模型。

## 配置

```bash
python3 -m agent --config /path/to/my.toml --list-models
```

默认配置位于：

```text
agent/config/defaults.toml
```

相对模型路径按进程当前目录解析。`main` 和 `embed` 不能通过 `--disable` 关闭。

## RAG

RAG 是本地闭环：

```text
docs/ 本地 Markdown
  → section 切块
  → 本地 embedding
  → SQLite vector + FTS5
  → RRF/启发式重排
  → section 父块返回
```

当前没有网页搜索、爬虫或搜索 API。

## 测试

```bash
python3 -m compileall -q agent tests start.py
python3 tests/run_all.py
```

## 已知边界

- 主脑不能自动修改源码、自动合并提案或更新权重
- 自进化流程停在“提案 + `Agents_opinion` 归档”
- 没有网页搜索
- 单 GPU 下同一时刻只常驻一个模型
- 本地 Q4 模型输出存在方差，评审门禁不能保证所有任务一次通过

## 目录

```text
agent/                 harness 核心、适配器、工具、UI、配置
docs/                  架构、ADR、工作记录
tests/                 stdlib unittest
test_history/          人工测试档案（agent 不可读）
Agents_opinion/        主脑提案归档（agent 不可读）
start.py               跨平台启动器
start.sh/.command/.bat 各平台双击入口
```

## 文档

- 架构与进度：`docs/pre_architecture.md`
- ADR：`docs/decisions/`
- 运行记录：`docs/WORKLOG.md`
- 可选模型：`docs/decisions/ADR-042-optional-models.md`
- 双击启动器：`docs/decisions/ADR-043-double-click-launcher.md`
