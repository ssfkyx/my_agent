# ADR-036：Web 多模态附件上传

- **状态**：已采纳（2026-10-30）
- **实现状态**：已落地并实跑验证（2026-10-30）
- **关联代码**：`agent/ui/web.py`（`POST /upload`、附件注入、Attach 按钮/拖拽）、
  `agent/app.py`（上传根与媒体上限注入）、`agent/adapters/media.py`（ASR `<asr_text>` 归一化）、
  `agent/tools/perception.py`（`attach_file` / `describe_image` / `transcribe`）
- **关联 ADR**：ADR-020（感知工具与 mmproj 门禁）、ADR-027（Web UI）、ADR-035（token 预算）
- **上游文档**：`docs/pre_architecture.md` §9（UI）、§12（进度）

## 背景

ASR/VL 两个模型和 `perception.*` 工具早已存在，但 Web/TUI 输入层只接受文本：

- 页面只有 `#composer input`，没有文件选择框或拖拽区
- HTTP 路由只有 `/message`、`/consent`、`/cancel`、`/events`、`/health`
- 用户无法从浏览器把图片/音频送进 workspace，只能手动复制文件后输入路径

因此“多模态模型”只在手工路径工作流里可用，聊天窗口里看不到附件入口。

## 决策

1. **新增 `POST /upload`**：stdlib `email.parser` 解析 `multipart/form-data`，不引入第三方依赖。
2. **会话隔离落盘**：保存到 `<write_root>/uploads/<session_id>/<uuid>_<safe_name>`；
   `write_root` 默认是 `artifacts/`，因此仍受 ADR-026/028 的写边界约束。
3. **类型与大小门禁**：只接受
   - 图片：`.png/.jpg/.jpeg/.webp/.gif`，上限 `media.max_image_bytes`
   - 音频：`.wav/.mp3/.flac/.ogg/.m4a`，上限 `media.max_audio_bytes`
   文件名只取 basename 并白名单化，拒绝空文件、未知后缀、超限文件。
4. **附件路径校验**：`/message` 新增可选 `attachments: string[]`；后端用
   `Path.resolve()` 校验每个路径必须位于本会话 upload 目录内，否则 400。
5. **主脑提示注入**：提交时把附件块拼进 user text：
   ```text
   [attachments]
   - artifacts/uploads/<sid>/<file> (image|audio)
   Use perception.attach_file on each attachment, then perception.describe_image
   for images or perception.transcribe for audio before answering.
   ```
   这样主脑在 planner 层看到明确路径与工具选择，不需要 UI 直接调模型。
6. **前端**：`Attach` 按钮 + 隐藏 `input[type=file]` + 拖拽区 + 附件 chips；
   发送时只传路径，不传 base64。附件上传完成前禁用发送。
7. **ASR 输出归一化**：Qwen3-ASR 返回 `language English<asr_text>...`，
   `_normalise_asr_text()` 只保留 marker 之后正文；静音得到空串而不是
   `language None<asr_text>`，也不再被当成上游错误。

## 理由

- 复用现有 `perception.*` 工具链，UI 不直接碰模型，保持 ADR-006 的 UI 只消费信封原则。
- 只传路径而不是 base64：避免 `/message` 体积暴涨、ledger 记录二进制、上下文被媒体数据污染。
- 会话目录 + resolve 校验：防止浏览器文件名穿越、跨会话读取、把任意 workspace 文件当附件。
- 明确工具提示比依赖模型“自己猜”更可靠；实测 planner 会按顺序调用
  `perception.attach_file` → `perception.describe_image` / `perception.transcribe`。

## 代价与限制

- 上传文件长期保留在 `artifacts/uploads/`，尚无 TTL/清理任务；需要用户或后续任务清理。
- 只覆盖 Web；TUI 仍是文本输入（TUI 可手动输入工作区路径）。
- 媒体调用会切换常驻模型（main → vl/asr → main → sub），有额外加载延迟；
  不能与主脑并行。
- 附件只允许图片/音频；文本附件仍走 `workspace.read_file`，不在此入口。
- 未做病毒扫描/内容嗅探；扩展名是分类依据，实际解码由感知模型负责。

## 验证方式与结果

**自动化（stdlib unittest）**

```text
python3 tests/run_all.py
Ran 11 tests ... OK
```

新增覆盖：

- `tests/test_web_upload.py`：上传落盘、消息注入、未知后缀、超限、跨会话路径拒绝
- `tests/test_media_asr.py`：`<asr_text>` 前缀剥离、空转写、无 marker 原样返回

**实跑（Web UI，2026-10-30）**

| # | 用例 | 结果 |
| --- | --- | --- |
| 1 | 上传 `probe.png` + “Describe the attached image.” | `t-20261030-201402-25a3e3`：planner 调 `perception.attach_file` → `perception.describe_image`；VL 回答“solid blue color”；`models={'main':99,'vl':None,'sub':99}`；`review=PASS`；35.66 s |
| 2 | 上传静音 `silence.wav` + “Transcribe the attached audio.” | `t-20261030-201705-062f82`：planner 调 `perception.attach_file` → `perception.transcribe`；`data.text=""`；答案说明无语音；`models={'main':99,'asr':None,'sub':99}`；`review=PASS`；33.95 s |
| 3 | 页面结构 | 存在 `#attach`、`#file`、`#attachments`，`accept="image/*,audio/*,..."` |
| 4 | 退出清理 | 测试结束无残留 `llama-server`，8090/8081/8084/8765 端口释放 |

> 备注：第一次图像测试曾因外部遗留的 8081 `llama-server` 触发
> `ModelBusyError`；清理后重跑通过。该错误说明 supervisor 的“拒绝附着未知模型”
> 保护正常，不是上传功能问题。
