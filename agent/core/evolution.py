"""evolution — Phase 1 optimizer 提案协议（只提案，不自动应用）。

Module: agent.core.evolution
Responsibility: 定义结构化 ChangeProposal、JSONL 提案队列、失败日志和只读报告。
Depends on: stdlib (json, time, uuid, pathlib, dataclasses)
Depended by: tools.evolution, harness（失败日志）, __main__（--evolve）
ADRs: ADR-039（Phase 0 观察层）, ADR-040（Phase 1 optimizer 协议）
"""

import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

TARGET_KINDS = ("prompt", "tool_schema", "tool_impl", "config", "test", "docs")
RISK_LEVELS = ("low", "medium", "high")
SCHEMA_VERSION = 2
_MAX_TEXT = 12000
_MAX_SHORT = 1000


class EvolutionError(ValueError):
    """提案或失败记录不合法。"""


@dataclass(frozen=True, slots=True)
class Alternative:
    """一个候选方案及其取舍。"""

    option: str
    description: str
    tradeoff: str


@dataclass(frozen=True, slots=True)
class ExpectedMetric:
    """可测量的预期效果，不允许只写“更好”。"""

    metric: str
    baseline: str
    target: str
    method: str


@dataclass(frozen=True, slots=True)
class ChangeProposal:
    """主脑提出的一条 harness 变更实验；永远不会在 turn 内自动生效。"""

    proposal_id: str
    schema_version: int
    target_kind: str
    target: str
    current_behavior: str
    root_cause: str
    alternatives: tuple[Alternative, ...]
    proposal: str
    expected_metric: ExpectedMetric
    replay_plan: tuple[str, ...]
    risk: str
    rollback: str
    status: str
    created_at: float
    source_call: str

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any], *, source_call: str = "") -> "ChangeProposal":
        if not isinstance(payload, Mapping):
            raise EvolutionError("proposal must be an object")
        target_kind = _required_text(payload.get("target_kind"), "target_kind")
        if target_kind not in TARGET_KINDS:
            raise EvolutionError(f"target_kind must be one of {TARGET_KINDS}")
        target = _required_text(payload.get("target"), "target", 500)
        current_behavior = _required_text(payload.get("current_behavior"), "current_behavior", 1500)
        root_cause = _required_text(payload.get("root_cause"), "root_cause", 1500)
        proposal = _required_text(payload.get("proposal"), "proposal", _MAX_TEXT)
        risk = _required_text(payload.get("risk"), "risk")
        if risk not in RISK_LEVELS:
            raise EvolutionError(f"risk must be one of {RISK_LEVELS}")
        rollback = _required_text(payload.get("rollback"), "rollback", 1500)
        alternatives = _alternatives(payload.get("alternatives"))
        expected_metric = _expected_metric(payload.get("expected_metric"))
        replay_plan = _text_list(payload.get("replay_plan"), "replay_plan", minimum=1)
        now = time.time()
        return cls(
            proposal_id=f"p-{int(now)}-{uuid.uuid4().hex[:8]}",
            schema_version=SCHEMA_VERSION,
            target_kind=target_kind,
            target=target,
            current_behavior=current_behavior,
            root_cause=root_cause,
            alternatives=alternatives,
            proposal=proposal,
            expected_metric=expected_metric,
            replay_plan=replay_plan,
            risk=risk,
            rollback=rollback,
            status="proposed",
            created_at=now,
            source_call=source_call,
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "schema_version": self.schema_version,
            "target_kind": self.target_kind,
            "target": self.target,
            "current_behavior": self.current_behavior,
            "root_cause": self.root_cause,
            "alternatives": [
                {
                    "option": alternative.option,
                    "description": alternative.description,
                    "tradeoff": alternative.tradeoff,
                }
                for alternative in self.alternatives
            ],
            "proposal": self.proposal,
            "expected_metric": {
                "metric": self.expected_metric.metric,
                "baseline": self.expected_metric.baseline,
                "target": self.expected_metric.target,
                "method": self.expected_metric.method,
            },
            "replay_plan": list(self.replay_plan),
            "risk": self.risk,
            "rollback": self.rollback,
            "status": self.status,
            "created_at": self.created_at,
            "source_call": self.source_call,
        }


class EvolutionStore:
    """只追加的提案队列；Phase 1 仍然没有 apply 方法。"""

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def queue(self, proposal: ChangeProposal) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(proposal.to_payload(), ensure_ascii=False) + "\n")

    def load(self) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        entries: list[dict[str, Any]] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                entries.append(data)
        return entries


def append_failure(path: Path, payload: Mapping[str, Any]) -> None:
    """追加一条 ERROR 记录；失败日志本身不能阻断 turn。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = dict(payload)
    entry.setdefault("created_at", time.time())
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            entries.append(data)
    return entries


def build_evolution_report(*, proposals_path: Path, failures_path: Path) -> dict[str, Any]:
    """生成只读观察报告；不修改任何提案或代码。"""
    proposals = load_jsonl(proposals_path)
    failures = load_jsonl(failures_path)
    by_kind: dict[str, int] = {}
    by_risk: dict[str, int] = {}
    for proposal in proposals:
        kind = str(proposal.get("target_kind", "unknown"))
        risk = str(proposal.get("risk", "unknown"))
        by_kind[kind] = by_kind.get(kind, 0) + 1
        by_risk[risk] = by_risk.get(risk, 0) + 1
    by_code: dict[str, int] = {}
    for failure in failures:
        code = str(failure.get("code", "unknown"))
        by_code[code] = by_code.get(code, 0) + 1
    return {
        "proposals_path": str(proposals_path),
        "failures_path": str(failures_path),
        "proposal_count": len(proposals),
        "failure_count": len(failures),
        "proposals_by_kind": by_kind,
        "proposals_by_risk": by_risk,
        "failures_by_code": by_code,
        "latest_proposals": proposals[-5:],
        "latest_failures": failures[-5:],
    }


def format_evolution_report(report: Mapping[str, Any]) -> str:
    lines = [
        f"proposals: {report.get('proposal_count', 0)}  ({report.get('proposals_path')})",
        f"failures:  {report.get('failure_count', 0)}  ({report.get('failures_path')})",
        f"by kind:   {json.dumps(report.get('proposals_by_kind', {}), ensure_ascii=False)}",
        f"by risk:   {json.dumps(report.get('proposals_by_risk', {}), ensure_ascii=False)}",
        f"by code:   {json.dumps(report.get('failures_by_code', {}), ensure_ascii=False)}",
    ]
    latest_proposals = report.get("latest_proposals", [])
    if latest_proposals:
        lines.append("latest proposals:")
        for proposal in latest_proposals:
            metric = proposal.get("expected_metric", {})
            metric_text = ""
            if isinstance(metric, Mapping) and metric.get("metric") and metric.get("target"):
                metric_text = f" metric={metric.get('metric')}->{metric.get('target')}"
            lines.append(
                f"  - {proposal.get('proposal_id')} [v{proposal.get('schema_version', 1)}]"
                f" [{proposal.get('risk')}] {proposal.get('target_kind')}: {proposal.get('target')}"
                f" status={proposal.get('status')}{metric_text}"
            )
    latest_failures = report.get("latest_failures", [])
    if latest_failures:
        lines.append("latest failures:")
        for failure in latest_failures:
            lines.append(
                f"  - {failure.get('turn_id')} {failure.get('code')}: "
                f"{str(failure.get('message', ''))[:160]}"
            )
    return "\n".join(lines)


def _alternatives(value: Any) -> tuple[Alternative, ...]:
    if not isinstance(value, list):
        raise EvolutionError("alternatives must be an array")
    if len(value) < 2:
        raise EvolutionError("alternatives must contain at least 2 options")
    if len(value) > 5:
        raise EvolutionError("alternatives must contain at most 5 options")
    result: list[Alternative] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping):
            raise EvolutionError("alternatives items must be objects")
        option = _required_text(item.get("option"), "alternatives.option", 100)
        if option in seen:
            raise EvolutionError(f"duplicate alternative option: {option}")
        seen.add(option)
        result.append(
            Alternative(
                option=option,
                description=_required_text(item.get("description"), "alternatives.description", 800),
                tradeoff=_required_text(item.get("tradeoff"), "alternatives.tradeoff", 800),
            )
        )
    return tuple(result)


def _expected_metric(value: Any) -> ExpectedMetric:
    if not isinstance(value, Mapping):
        raise EvolutionError("expected_metric must be an object")
    return ExpectedMetric(
        metric=_required_text(value.get("metric"), "expected_metric.metric", 200),
        baseline=_required_text(value.get("baseline"), "expected_metric.baseline", 500),
        target=_required_text(value.get("target"), "expected_metric.target", 500),
        method=_required_text(value.get("method"), "expected_metric.method", 1000),
    )


def _required_text(value: Any, field_name: str, limit: int = _MAX_SHORT) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvolutionError(f"{field_name} must be a non-empty string")
    text = value.strip()
    if len(text) > limit:
        raise EvolutionError(f"{field_name} must be at most {limit} characters")
    return text


def _text_list(value: Any, field_name: str, *, minimum: int = 0) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise EvolutionError(f"{field_name} must be an array")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise EvolutionError(f"{field_name} items must be non-empty strings")
        result.append(item.strip()[:300])
    if len(result) < minimum:
        raise EvolutionError(f"{field_name} must contain at least {minimum} item(s)")
    if len(result) > 10:
        raise EvolutionError(f"{field_name} must contain at most 10 items")
    return tuple(result)
