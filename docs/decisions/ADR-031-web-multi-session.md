# ADR-031：Web 多会话隔离（单模型串行）

- **状态**：已采纳（2026-10-30）
- **实现状态**：已落地（2026-10-30）
- **关联代码**：`agent/ui/web.py:WebUI/_Session`、`agent/app.py:_run_web_sessions`
- **关联 ADR**：ADR-027（Web UI）、ADR-029（token/TLS/Markdown）、ADR-030（取消）
- **上游文档**：`docs/pre_architecture.md` §9（Web UI）

## 背景

ADR-027 的 Web UI 只有一个全局输入队列/事件流/同意状态：两个浏览器同时使用时，事件会
串台，一个会话的 `/quit` 或 consent 会影响另一个。用户要求“多会话隔离，但仍以一次只
调用一个模型为原则”。

## 决策

1. **会话标识**：页面为每个 tab 生成 `sessionStorage["agent-session"]`（`crypto.randomUUID`，
   无则退化为时间戳+随机数）；API 用 `X-Agent-Session` header，SSE 用 `?session=` query。
   缺省/非法 id 落到 `default`（兼容旧测试/单页面）。
2. **每会话独立状态**：`_Session` 持有：
   - SSE 事件历史（最多 `max_events`）与订阅者集合；
   - 兼容输入队列、`active`（已排队或运行）、`running`（正在执行）；
   - 独立的 consent pending/event/result。
3. **全局串行**：所有 `/message` 进入一个全局 `_jobs` 队列；`App._run_web_sessions`
   逐条 `next_job()`，每次只调用一次 `run_once`。模型/工具调用不并发。
4. **活动会话路由**：`begin_turn(sid)` 设置 `_active_sid`；`render/report/ask_consent/
   show_error` 只把事件推给该会话；`end_turn(sid)` 清除活动标记。
5. **会话内单 turn**：同一会话已有 queued/running turn 时 `/message` 返回 409；
   不同会话可以各自排队，但执行仍串行。
6. **会话级控制**：
   - `/cancel` 只允许 `running` 的会话调用；
   - `/consent` 只影响该会话的 pending consent（其他会话返回 409）；
   - `/quit` 只关闭该会话（发 `session_closed`），不停止整个 Web 服务；
   - 页面 “New” 按钮清除 `agent-session` 并刷新，得到新会话。
7. **认证仍由 token 负责**：session id 是会话能力标识，不是认证凭据；配置 token 后
   所有 API 仍先校验 Bearer/query token。

## 理由

- 会话 id 放在 `sessionStorage` 而不是 cookie，能让同一浏览器的多个 tab 各自隔离。
- 全局 job 队列让“多会话隔离”和“单模型串行”同时成立，不需要线程池/并发模型。
- 每个会话独立 consent/event 历史，避免串台；`/quit` 不再误杀其他会话。

## 代价与限制

- 隔离是 **UI/队列级**：`RoleRegistry`、`ProgressWatch`、memory、模型进程仍是进程级；
  一个会话切换 role 会影响后续会话的默认 role。真正的 per-session role/ledger 需要
  per-session Harness/RoleRegistry（另行决策）。
- 排队中的会话只显示 busy，没有队列位置/预计等待时间。
- 会话对象只增不删（页面关闭后仍保留到进程结束）；长期运行需要 TTL/清理策略。
- 单进程仍是“一次一个模型调用”，吞吐量受模型速度限制。

## 验证方式

- 两个 session（`s1`/`s2`）分别 `/message`：都入队，`next_job` 按提交顺序返回；
  `begin_turn("s1")` 后 envelope 只进 s1 历史，s2 不受影响。
- `s1` busy 时再提交 → 409；`s2` 独立提交 → 200。
- consent：`begin_turn("s1")` + `ask_consent`，向 s2 提交 `/consent` → 409；
  向 s1 提交 → 200 且 ask_consent 返回 True。
- App 层：fake `run_once` 下两个 session 的 job 串行执行，顺序为提交顺序；
  `/quit` 只关闭对应 session。
- 旧 Web 直测（无 session header）仍落到 default，全部通过。
