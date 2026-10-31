"""agent — 本地单模型顺序流水线 agent。

Module: agent
Responsibility: 包边界与版本标识；不在导入期装配任何组件（避免循环依赖与隐式副作用）。
Depends on: stdlib only
Depended by: python -m agent、外部脚本
ADRs: ADR-003（零第三方依赖）, ADR-006（TB-0 范围）
"""

__all__ = ["__version__"]

__version__ = "0.1.0-tb0"
