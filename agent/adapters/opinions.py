"""opinions — 把主脑提出的 harness 修改方向归档到 Agents_opinion。

Module: agent.adapters.opinions
Responsibility: 将 ChangeProposal（v1/v2）渲染成人类可读的 Markdown，并维护索引。
Depends on: stdlib (datetime, pathlib, re)
Depended by: tools.evolution, app, 迁移脚本
ADRs: ADR-041（Agents_opinion 实践档案）
"""

import re
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping


class OpinionLog:
    """只追加的主脑意见档案；不执行任何提案。"""

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    @property
    def directory(self) -> Path:
        return self._directory

    def record(self, proposal: Mapping[str, Any]) -> Path:
        """写一条 Markdown；同一 proposal_id 已存在时直接返回。"""
        self._directory.mkdir(parents=True, exist_ok=True)
        proposal_id = str(proposal.get("proposal_id") or "unknown")
        path = self._directory / f"{self._stamp(proposal)}-{_safe(proposal_id)}.md"
        if path.exists():
            return path
        path.write_text(self._render(proposal), encoding="utf-8")
        self._append_index(proposal, path)
        return path

    def _append_index(self, proposal: Mapping[str, Any], path: Path) -> None:
        index = self._directory / "INDEX.md"
        if not index.exists():
            index.write_text(
                "# Agents_opinion\n\n"
                "主脑提出的 harness 修改方向归档；只记录，不自动应用。\n\n"
                "## 记录\n\n",
                encoding="utf-8",
            )
        proposal_id = str(proposal.get("proposal_id") or "unknown")
        target = str(proposal.get("target") or "unknown")
        kind = str(proposal.get("target_kind") or "unknown")
        risk = str(proposal.get("risk") or "unknown")
        with index.open("a", encoding="utf-8") as handle:
            handle.write(
                f"- `{proposal_id}` [{kind}/{risk}] {target} "
                f"([{path.name}]({path.name}))\n"
            )

    @staticmethod
    def _stamp(proposal: Mapping[str, Any]) -> str:
        raw = proposal.get("created_at")
        if isinstance(raw, (int, float)):
            return datetime.fromtimestamp(float(raw)).strftime("%Y%m%d-%H%M%S")
        return datetime.now().strftime("%Y%m%d-%H%M%S")

    @staticmethod
    def _render(proposal: Mapping[str, Any]) -> str:
        lines = [
            f"# Agents_opinion: {proposal.get('target', 'unknown')}",
            "",
            "| 字段 | 值 |",
            "| --- | --- |",
            f"| proposal_id | `{proposal.get('proposal_id', 'unknown')}` |",
            f"| schema_version | {proposal.get('schema_version', 1)} |",
            f"| target_kind | `{proposal.get('target_kind', 'unknown')}` |",
            f"| target | `{proposal.get('target', 'unknown')}` |",
            f"| risk | `{proposal.get('risk', 'unknown')}` |",
            f"| status | `{proposal.get('status', 'proposed')}` |",
            "",
            "## 当前行为",
            "",
            str(proposal.get("current_behavior") or proposal.get("rationale") or "（未记录）"),
            "",
            "## 根因",
            "",
            str(proposal.get("root_cause") or "（未记录）"),
            "",
            "## 备选方案",
            "",
        ]
        alternatives = proposal.get("alternatives")
        if isinstance(alternatives, list) and alternatives:
            for index, alternative in enumerate(alternatives, start=1):
                if isinstance(alternative, Mapping):
                    lines.extend(
                        [
                            f"### {index}. {alternative.get('option', 'option')}",
                            "",
                            str(alternative.get("description", "")),
                            "",
                            f"- 取舍：{alternative.get('tradeoff', '')}",
                            "",
                        ]
                    )
                else:
                    lines.extend([f"- {alternative}", ""])
        else:
            lines.extend(["- （v1 提案未记录 alternatives）", ""])

        lines.extend(["## 选中的提案", "", str(proposal.get("proposal", "")), ""])

        metric = proposal.get("expected_metric")
        lines.extend(["## 预期指标", ""])
        if isinstance(metric, Mapping):
            lines.extend(
                [
                    f"- 指标：{metric.get('metric', '')}",
                    f"- 基线：{metric.get('baseline', '')}",
                    f"- 目标：{metric.get('target', '')}",
                    f"- 方法：{metric.get('method', '')}",
                ]
            )
        else:
            lines.append(str(proposal.get("expected_effect") or "（未记录）"))
        lines.append("")

        replay = proposal.get("replay_plan") or proposal.get("tests") or []
        lines.extend(["## 回放计划", ""])
        if isinstance(replay, list) and replay:
            lines.extend([f"- {item}" for item in replay])
        else:
            lines.append("- （未记录）")
        lines.extend(
            [
                "",
                "## 回滚",
                "",
                str(proposal.get("rollback") or "（未记录）"),
                "",
                "---",
                "",
                "本文件只是主脑意见记录，不代表提案已执行或已批准。",
                "",
            ]
        )
        return "\n".join(lines)


def _safe(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-") or "proposal"
