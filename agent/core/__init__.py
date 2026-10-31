"""core — 纯领域层：协议、状态、计划、编排、评审与报告。

Module: agent.core
Responsibility: 只依赖 stdlib 与同层模块；不 import 任何 adapter/ui/config（保持可测试与可替换）。
Depends on: stdlib only
Depended by: adapters, tools, ui, app
ADRs: ADR-003（零依赖）, ADR-006（分层边界）, ADR-013（评审与报告）
"""

__all__: list[str] = []
