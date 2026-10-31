# ADR-027：stdlib HTTP + SSE Web UI

- **状态**：已采纳（2026-10-30）
- **实现状态**：已落地（2026-10-30）
- **关联代码**：`agent/ui/web.py:WebUI/_WebHandler`、`agent/app.py`（`ui_mode="web"`、
  `web_url`）、`agent/config.py:WebSettings`、`agent/__main__.py:--web`
- **关联 ADR**：ADR-003（stdlib only）、ADR-006（UI 只消费 Envelope）、ADR-021（同意询问）、
  ADR-025（第二个 UI 消费者）、ADR-029（token/TLS/Markdown）
- **上游文档**：`docs/pre_architecture.md` §9（先 TUI 后 Web）

## 背景

TUI 是同步单窗口界面，适合本机终端；但查看长答案、手机/异地查看、复制内容仍不方便。
核心协议、Presenter 与 `App.run_session` 已经稳定，可以增加第三个 `UiPort` 消费者，
而不改动 Harness/Tool/模型适配器。

## 决策

1. **只用 stdlib**：`ThreadingHTTPServer` + `BaseHTTPRequestHandler` + SSE，不引入 Web 框架、
   不引入 WebSocket。页面是内嵌的单个 HTML/CSS/JS 字符串。
2. **WebUI 是第三消费者**：实现 `UiPort` + `InteractiveUiPort`；只消费 `Envelope`，
   只把浏览器输入/同意决定回传给 `App.run_session`，不 import `core.harness`。
3. **路由最小化**：
   - `GET /`：单页 HTML；
   - `GET /events`：SSE 事件流；
   - `POST /message`：`{"text": "..."}` 提交一条用户消息；
   - `POST /consent`：`{"decision": true|false}` 回答同意问题；
   - `GET /health`：存活检查。
4. **SSE 单向推送 + POST 回传**：服务端把 `Envelope` 包成
   `{"type":"envelope","envelope":{...}}`；另有 `state/report/consent/consent_done/error` UI 事件。
   SSE 连接建立时回放最近 500 条事件，浏览器按 `turn_id|kind|message_id|seq` 去重。
5. **同意询问在 turn 线程内阻塞**：`ask_consent` 推送 `consent` 事件并等待
   `POST /consent`；只有 `true` 才允许外发。`--consent allow/deny` 时仍不弹窗，
   由 Harness 发出最终 `CONSENT_REQUEST` 事件。
6. **单 turn 串行**：`submit_message` 在 turn 运行期间返回 409，避免并行/覆盖输入；
   `report`/`show_error` 将 busy 复位。输入队列与 `App.run_session` 的阻塞读取配合，
   不改变 core 的 turn 状态机。
7. **默认只绑定回环**：`[web] host = "127.0.0.1"`、`port = 8765`；默认无认证/TLS，
   可配置 Bearer token 与 TLS（ADR-029）；未加保护时不要改成 `0.0.0.0`。
8. **CLI 显式选择**：`--web` 与 `--once/--tui` 互斥，也不需要 TTY；`--json` 只用于单轮，
   不能与 `--web` 组合；启动后把 URL 打到 stderr。

## 理由

- stdlib 方案与 TUI 一致，零第三方依赖，部署简单；SSE 足够表达单向事件流。
- WebUI 复用 `Presenter` 的 token 名和 `App.run_session`，避免为 Web 另写一套编排。
- 单 turn 串行 + 409 让“一个页面一个 turn”的语义清晰；需要并发时再引入队列/会话隔离。
- 事件回放让刷新页面后仍能看到本轮上下文；去重避免 SSE 自动重连造成重复行。

## 代价

- 默认无认证、无 TLS；ADR-029 增加可选 Bearer token/TLS，未配置时仍只适合本机。
- SSE 是单向事件流；ADR-030 增加 `POST /cancel`（协作式取消，不能中断阻塞中的模型调用）。
- 历史最多 500 条，完整记录仍在 `runs/*.jsonl`；ADR-029 增加安全子集 Markdown 渲染。
- 每个 SSE 连接占用一个线程（`ThreadingHTTPServer`），客户端很多时需要换异步服务器。
- 单页面单 turn；ADR-031 已增加多会话隔离（单模型串行），但 role/memory 仍是进程级。

## 验证方式

- `WebUI` 直测（`/tmp/check_web.py`）：
  - `/health` 与 `/` 可访问；
  - `POST /message` 唤醒 `read_user_input`，turn 运行中再次提交返回 409；
  - `report` 后 busy 复位，可再次提交；
  - `ask_consent` + `POST /consent` 的 y/N 往返；
  - `/events` 能收到 JSON 事件。
- 实跑 `python -m agent --web --consent deny`：
  - 页面连接 SSE 后提交 `ping`，收到
    `USER_INPUT → PLAN → TOOL_CALL → TOOL_RESULT → REVIEW → REFLECTION → ANSWER → RUN_REPORT`；
  - run `t-20261030-152113-e63ea4`，44.35 s，`retries=0 failures=0`。
- CLI 校验：`--web` 与 `--once/--tui` 互斥；`--web --json` 报参数错误。
