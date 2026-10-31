"""tools — 主 agent 可调用的工具实现与注册表。

Module: agent.tools
Responsibility: 每个模块一类工具（ADR-006 的 7 大类），全部实现 agent.ports.Tool。
Depends on: stdlib + agent.core.protocol + agent.ports
Depended by: app
ADRs: ADR-001（ToolSpec/ToolCall/ToolResult 形状）, ADR-023（switch_role）,
    ADR-024（delegate.draft / delegate.review）, ADR-026（workspace 写/执行策略）,
    ADR-028（bwrap 命令沙箱）, ADR-032（模型自选命令 profile）, ADR-039（演进提案）
"""

__all__: list[str] = []
