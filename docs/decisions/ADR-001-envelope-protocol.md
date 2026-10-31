# ADR-001：以 Envelope 作为唯一组件协议

- **状态**：已采纳（2026-10-29）
- **关联代码**：`agent/core/protocol.py:Envelope`、`Envelope.to_json`、`Envelope.from_json`、`agent/core/turn.py:TurnLedger.append`、`agent/core/protocol.py:ToolCall`、`ToolResult`、`ErrorInfo`
- **关联 ADR**：ADR-002（模型调用边界）、ADR-006（TB-0 的 kind 裁剪）
- **上游文档**：`docs/pre_architecture.md` §5.1、§5.2

## 背景

需求写明"所有组件间通信格式统一为 JSON，接口统一"。若 UI、工具、模型、记忆各用一套数据结构，那么每新增一个消费者（TUI → Web、本地 → 场外 API）都要重新对一遍语义，追溯成本随组件数平方增长。

## 决策

1. 跨组件/跨进程边界只传 `Envelope`：`kind / message_id / turn_id / sender / payload / meta` 六个字段，`payload` 按 `kind` 定 schema。
2. `meta` 只放旁路信息（`seq`、`ts`、`role`），**不得**承载语义；语义一律进 `payload`。
3. 工具调用统一为 `ToolCall → ToolResult`，错误统一为 `ErrorInfo{code,message,retryable,hint}`，`code` 取固定枚举。
4. 编解码集中在 `Envelope.to_json/from_json`：缺字段、空字符串、非法 `kind`、`payload` 非对象一律抛 `ProtocolError`（子类 `ValueError`）。
5. `seq` 由 `TurnLedger.append` 统一分配（`m-0001` 递增），调用方不得自带序号。

## 理由

- 单一信封 = 单一序列化点，UI/工具/日志/报告共享同一事实来源；`TurnLedger` 因此可以既是运行时状态又是审计材料。
- 严格校验把"畸形消息"挡在边界上，而不是让错误在 `payload["data"]["hits"]` 这类深层路径上晚炸。
- `meta` 与 `payload` 分离，使"时间戳变了但语义没变"这类差异不会污染比较与去重。

## 代价

- 每次工具调用多一层 dict 包装（可忽略）。
- 强校验使新增 `kind` 必须同步改 `MessageKind` 与 codec（有意为之：强制显式扩展）。
- `payload` 仍是 `Mapping[str, Any]`，深层 schema 校验要等 §5.3 的 Plan/Review 契约落地（TB-1 起由 `Plan.from_payload` 承担）。

## 验证方式

`python -m compileall` 保证可导入；`Envelope.from_json` 的非法输入路径在本地断言脚本中覆盖（缺字段、未知 kind、payload 非 dict）。
