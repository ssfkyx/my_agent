"""ui — 展示层：主题、投影、行式控制台、curses TUI 与 HTTP/SSE Web UI。

Module: agent.ui
Responsibility: 只消费 Envelope 与 Theme token，不 import core.harness 之外的状态。
Depends on: stdlib + agent.core.protocol
Depended by: app
ADRs: ADR-006（UI 只消费信封）, ADR-025（curses TUI 第二消费者）,
    ADR-027（HTTP/SSE Web UI 第三消费者）, ADR-029（Web token/TLS/Markdown）,
    ADR-030（协作式取消与 TUI 滚动）, ADR-031（Web 多会话）
"""

__all__: list[str] = []
