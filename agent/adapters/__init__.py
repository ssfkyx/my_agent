"""adapters — 对外部系统的实现层（llama.cpp、后续的 HandleToMaster / 向量库 / 媒体）。

Module: agent.adapters
Responsibility: 实现 ports 中的协议；不被子模块在导入期自动装配。
Depends on: stdlib + agent.ports + agent.config
Depended by: app
ADRs: ADR-002（模型进程管理）, ADR-004（BrainPort 实现）
"""

__all__: list[str] = []
