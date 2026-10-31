# ADR-020：媒体输入作为"感知工具"，并以 mmproj 门禁保护

- **状态**：已采纳（2026-10-30）
- **关联代码**：`agent/ports.py:MediaPort`、`MediaResult`、`MediaError`、`agent/adapters/media.py:LlamaCppMedia`、`LlamaCppMedia.transcribe`、`LlamaCppMedia.describe_image`、`agent/tools/perception.py:TranscribeTool`、`DescribeImageTool`、`agent/config.py:MediaSettings`
- **关联 ADR**：ADR-002（单常驻）、ADR-014（role 路由）、ADR-017（嵌入同类做法）、ADR-009（路径沙箱）、ADR-006（工具大类 `perception`）
- **上游文档**：`docs/pre_architecture.md` §3 TB-5、§6.1（工具大类）、§11.1（asr/vl alias 与端口）

## 背景

需求要求"接受语音与图片"。有两种接法：

- **A. 前置转写**：CLI 收到 `--audio/--image` 后先转成文本，再当普通 `user_text` 交给 harness；
- **B. 感知工具**：把转写/看图做成 `perception.transcribe` / `perception.describe_image`，由主脑在计划里调用。

同时有一个真实环境风险：VL/ASR 必须带 `mmproj`（投影层）才能处理媒体；`config` 里若漏配，
服务要么起不来，要么静默退化成文本模型——这正是"看起来成功、实际没看图"的失败模式。

## 决策

1. 采用 **B（感知工具）**：媒体处理进入既有工具大类 `perception`，因此复用同一套
   `Guardrail`（路径沙箱）、`ToolResult`（失败即数据）、`RunReport`（耗时与失败码）与 `Review`（可评审）。
2. `MediaPort` 只有两个方法：`transcribe(path)` 与 `describe_image(path, prompt)`，均返回
   `MediaResult{text, model_alias, elapsed_s}`；**不做流式**（与 ADR-004 一致）。
3. `mmproj` 是**硬门禁**：`LlamaCppMedia` 在 ensure 之前检查 `profile.mmproj`，缺失或文件不存在时
   立即抛 `MediaError(code=MISSING_MMPROJ)`，绝不启动一个"只能读文本"的退化服务。
4. 传输格式用 llama.cpp 的 OpenAI 兼容多模态消息：
   音频 `{"type":"input_audio","input_audio":{"data":"<base64>","format":"wav"}}`，
   图片 `{"type":"image_url","image_url":{"url":"data:<mime>;base64,..."}}`。
5. 尺寸上限进配置（音频 25 MiB、图片 10 MiB），超限返回 `BAD_ARGS` 而不是把请求扔给服务端超时。
6. `temperature=0.0`：转写与看图要的是"描述事实"，不是创作。

## 理由

- 选 B 而不是 A，是因为"要不要看图、看哪张图"应由计划决定（用户可能只是提到某个文件名），
  前置转写会在模型还没决策时就花掉一次换模型的代价。
- mmproj 门禁放在适配器而不是配置校验：配置校验只能保证"路径写了"，只有适配器知道
  "这个模型真的能处理媒体"。两者都做，才是双保险。
- 复用工具面而不是新造一条通道，保证**新增能力不新增失败模式**：媒体失败同样是
  `TOOL_RESULT ok=false`，不会让 turn 崩掉。

## 代价

- 每次感知都会换模型（asr → 主脑、vl → 主脑），一次 turn 可能多出 2 次加载（各 3–8 s）。
- 媒体内容以 base64 进请求体，内存占用约为文件大小的 1.34 倍；上限已限制。
- 适配器依赖 llama.cpp 的多模态消息形状；不同 build 若不支持，会得到 `UPSTREAM_ERROR`（可见、不静默）。

## 验证方式

- 单测：`_data_url` 的 MIME/base64 正确；`profile.mmproj=None` 时 `transcribe` 抛 `MISSING_MMPROJ`；
  超限文件返回 `BAD_ARGS`；工具在成功路径把 `text` 放进 `data.text`。
- 实跑：在工作区生成一张纯色 PNG，`--once "describe docs/probe.png"` 期望
  `perception.describe_image` 成功且答案描述颜色；ASR 若无声样，则仅验证门禁与错误路径。
