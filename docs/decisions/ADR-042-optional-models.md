# ADR-042：启动时可选模型

- 状态：已落地（2026-10-31）
- 关联：ADR-002（单常驻）、ADR-020（媒体模型）、ADR-033（Q4 主脑）

## 背景

项目要上传 GitHub，其他人的模型文件和路径不一定与开发机相同。
原配置要求：

```text
main / sub / embed / asr / vl
```

全部存在且全部启用，否则启动失败。

## 决策

### 1. 必选与可选

必选：

```text
main      主脑
embed     RAG 嵌入
```

可选：

```text
sub       次脑 / review / reflection / delegate
asr       语音转写
vl        图片描述
llama31   预留，不参与当前编排
```

### 2. 启动参数

覆盖模型路径：

```bash
python3 -m agent --model main=/models/main.gguf \
                  --model embed=/models/embed.gguf \
                  --once "..."
```

禁用可选模型：

```bash
python3 -m agent --disable sub \
                  --disable asr \
                  --disable vl \
                  --once "..."
```

`ALIAS=none` 等价于禁用：

```bash
python3 -m agent --model sub=none --once "..."
```

查看模型清单：

```bash
python3 -m agent --list-models
python3 -m agent --list-models --json
```

### 3. 配置容错

- `[models.sub]` 等可选表缺失时，`review_alias` / media alias 自动视为未启用。
- 未启用的可选模型不要求 GGUF 存在。
- `--disable main` 或 `--disable embed` 直接报 `ConfigError`。
- `--model ALIAS=PATH` 只覆盖已存在的 `[models.<alias>]`，不凭空创建 profile。
- `--list-models` 显示 required/optional、角色、路径是否存在。

### 4. 运行时降级

无 `review_alias`：

- `Reviewer` / `Reflector` 为 `None`
- `Harness._review` / `_reflect` 返回 `None`
- `delegate.*` / `switch_role` 工具不注册
- `RUN_REPORT` 显示 `review=n/a`

无媒体 alias：

- 不构造 `LlamaCppMedia`
- 不注册 `perception.*`
- 主脑只使用文本工具

只有 ASR 或只有 VL：

- 构造媒体适配器
- 只注册对应模态的 perception 工具
- Web 上传按模态分别放行：无 VL 拒绝图片，无 ASR 拒绝音频

## 验证

```text
python3 tests/run_all.py
Ran 36 tests ... OK
```

主脑-only 烟雾测试：

```text
t-20261031-091304-97636a
--disable sub --disable asr --disable vl
ANSWER pong
17.62 s
review=n/a
retries=0 failures=0
```

`--list-models --disable sub --disable asr --disable vl` 显示：

```text
review: disabled
asr:    disabled
vl:     disabled
```

## 部署建议

GitHub 用户的最小启动：

```bash
python3 -m agent \
  --model main=/path/to/main.gguf \
  --model embed=/path/to/embed.gguf \
  --disable sub --disable asr --disable vl \
  --web
```

需要次脑时再加：

```bash
--model sub=/path/to/sub.gguf
```

需要多模态时再加 ASR/VL 及各自 mmproj。
