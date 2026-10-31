# ADR-029：Web UI 的 token、TLS 与安全 Markdown

- **状态**：已采纳（2026-10-30）
- **实现状态**：已落地（2026-10-30）
- **关联代码**：`agent/ui/web.py:WebUI/_WebHandler`、`agent/config.py:WebSettings`、
  `agent/app.py:_build_ui`
- **关联 ADR**：ADR-027（Web UI 基础）、ADR-006（UI 只消费 Envelope）、ADR-003（stdlib only）
- **上游文档**：`docs/pre_architecture.md` §9（Web 阶段）

## 背景

ADR-027 的 Web UI 默认只绑定回环，但没有认证、TLS 与 Markdown 渲染。一旦用户把 host
改为局域网地址，任何能访问端口的人都可以提交消息/回答同意；答案中的 Markdown 也只能
按纯文本显示。需要在 stdlib 约束内补上可选安全层，同时不破坏默认的本机体验。

## 决策

1. **Bearer token（可选）**：
   - `[web].auth_token_env` 指定环境变量名，默认 `AGENT_WEB_TOKEN`；变量为空则关闭认证。
   - `/` 页面本身公开（否则浏览器无法显示 token 输入框）；`/health`、`/events`、
     `/message`、`/consent`、`/cancel` 在配置 token 后都要求认证。
   - 支持 `Authorization: Bearer <token>`；`EventSource` 不能设置 header，因此
     `/events?token=...` 也接受 query token。
   - 失败返回 401 + `WWW-Authenticate: Bearer`；页面把 token 存 `sessionStorage`，
     提供 Token 按钮重新输入。
2. **TLS（可选）**：
   - `[web].tls_cert` / `[web].tls_key` 必须同时配置；路径按配置文件目录解析并校验存在。
   - 使用 `ssl.SSLContext(PROTOCOL_TLS_SERVER)` 包装 `ThreadingHTTPServer` socket；
     配置 TLS 后 `WebUI.url` 返回 `https://...`。
   - 不内置证书签发；自签名/反向代理证书由用户提供。
3. **安全子集 Markdown**：
   - 先对整段文本做 HTML 转义，再处理 fenced code、行内 code、`**bold**`、`#` 标题、
     `-` 列表与 `https?://` 链接；不渲染原始 HTML、不引入第三方 parser。
   - 链接只允许 `http/https`，加 `target="_blank" rel="noopener noreferrer"`。
   - 用户消息、错误消息仍按纯文本显示；只有 agent 答案走 Markdown。
4. **不改变默认行为**：未配置 token/TLS 时，`--web` 与 ADR-027 完全一致；
   认证/TLS 是显式配置项，不影响 `--once` / `--tui`。

## 理由

- 只依赖 stdlib，和 TUI/CLI 的部署方式一致；token 从环境变量读取，避免写进 TOML/版本库。
- 先转义再渲染是最小化 XSS 面的做法；不支持原始 HTML，避免引入 sanitizer 依赖。
- 页面公开 + API 认证让浏览器能先拿到 token 输入框，同时不暴露任何 agent 数据。

## 代价与限制

- token 存在 `sessionStorage`，同源脚本仍可读取；没有速率限制、审计和多用户账户。
- query token 可能进入浏览器历史；仅建议回环或受信 TLS 环境使用。
- Markdown 只支持常用子集，不保证与 CommonMark 完全一致。
- TLS 证书轮换/热加载未实现，修改后需重启 Web 服务。
- 多会话隔离仍未实现（需要 per-session Harness/并发模型，属于决策点）。

## 验证方式

- Auth：`GET /` 200；无 token 的 `/health`/`POST /message` 返回 401；
  `Authorization: Bearer secret` 与 `/health?token=secret` 返回 200；
  `/message` 带 token 可唤醒 `read_user_input`。
- TLS：自签名证书启动后 `url` 为 `https://`；用 `ssl._create_unverified_context`
  访问 `/health` 返回 200。
- 页面：包含 `renderMarkdown`、`token-modal`、`escapeHtml`；`node --check` 通过。
- `config.load` 默认 `auth_token_env=AGENT_WEB_TOKEN`、TLS 为空；只配 cert 或只配 key
  会在 `WebSettings` 校验失败。
