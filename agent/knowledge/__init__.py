"""knowledge — 本地 Markdown 的加载、分块与检索。

Module: agent.knowledge
Responsibility: 把 .md 流程文件切成带标题上下文的 chunk，并基于向量做 top-k 检索。
Depends on: stdlib + agent.ports + agent.core.protocol
Depended by: tools.knowledge, app
ADRs: ADR-016（分块与检索策略）, ADR-017（嵌入端口）, ADR-038（父块与 hybrid）
"""

__all__: list[str] = []
