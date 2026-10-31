"""turn — 一个 Turn 的生命周期状态与追加式 ledger。

Module: agent.core.turn
Responsibility: 定义 TurnState / Turn / TurnLedger；负责给信封分配 seq 与 message_id，
    并把一个 turn 的全部事件落成 JSONL。
Depends on: stdlib (dataclasses, datetime, enum, pathlib, secrets) + agent.core.protocol
Depended by: core.harness, app, ui.*
ADRs: ADR-001（seq 由 ledger 统一分配）, ADR-006（TB-0 只有单轮）
"""

import secrets
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping

from agent.core.protocol import Envelope, MessageKind


class TurnState(StrEnum):
    """Turn 的推进状态（对应 pre_architecture §4 状态机的 TB-0 子集）。

    ADR-006 只放行 TB-0 需要的五个状态；REVIEWING/REFLECTING 等随切片加入。
    """

    INGEST = "INGEST"
    PLANNING = "PLANNING"
    ACTING = "ACTING"
    ANSWERING = "ANSWERING"
    DELIVERED = "DELIVERED"
    FAILED = "FAILED"


class TurnLedger:
    """单个 Turn 的事件流水，也是唯一的事实来源。

    前置条件：turn_id 非空且在进程内唯一。
    后置条件：每次 append 都返回带递增 seq 与唯一 message_id 的信封。
    禁止事项：禁止在 append 之外构造带 seq 的信封；禁止并发 append（顺序流水线）。
    """

    def __init__(self, turn_id: str) -> None:
        self._turn_id = turn_id
        self._events: list[Envelope] = []

    @property
    def turn_id(self) -> str:
        """本 ledger 所属的 turn id。

        前置条件：ledger 由 Turn.__post_init__ 构造。
        后置条件：返回值与构造参数一致，只读。
        禁止事项：不要用它派生 message_id（那是 append 的职责）。
        异常：无。
        """
        return self._turn_id

    @property
    def next_seq(self) -> int:
        """下一个将被分配的序号（从 1 开始）。

        前置条件：无。
        后置条件：返回 len(events)+1；append 后自动递增。
        禁止事项：不要把它当"已用序号"传给外部（append 会再次 +1）。
        异常：无。
        """
        return len(self._events) + 1

    def append(
        self,
        kind: MessageKind,
        sender: str,
        payload: Mapping[str, Any] | None = None,
        **meta: Any,
    ) -> Envelope:
        """追加一条事件并返回它。

        前置条件：kind 为 MessageKind 成员；sender 非空。
        后置条件：返回的 Envelope.meta 至少含 seq（int）与 ts（float 秒，FP64）。
        禁止事项：不要重复 append 同一语义事件（例如 TOOL_RESULT 重放）。
        异常：ProtocolError（kind/sender/payload 不合法）。
        """
        seq = self.next_seq
        merged_meta: dict[str, Any] = {"seq": seq, "ts": datetime.now().timestamp()}
        merged_meta.update(meta)
        envelope = Envelope(
            kind=kind,
            message_id=f"m-{seq:04d}",
            turn_id=self._turn_id,
            sender=sender,
            payload=dict(payload or {}),
            meta=merged_meta,
        )
        self._events.append(envelope)
        return envelope

    def events(self) -> tuple[Envelope, ...]:
        """返回不可变的事件快照。

        前置条件：无。
        后置条件：返回元组，后续 append 不会改变它。
        禁止事项：不要把它当可变列表使用。
        异常：无。
        """
        return tuple(self._events)

    def to_transcript(self) -> list[dict[str, Any]]:
        """转为可序列化的转录列表。

        前置条件：无。
        后置条件：每个元素是 Envelope.to_json() 的结果，顺序与 append 顺序一致。
        禁止事项：不要在转录中丢弃 meta（排障依赖 seq）。
        异常：无。
        """
        return [envelope.to_json() for envelope in self._events]

    def save(self, path: Path) -> Path:
        """把全部事件写成 JSONL 文件（覆盖写，一个 turn 一个文件）。

        前置条件：path 的父目录可写。
        后置条件：文件内容为逐行 Envelope JSON，行序等于事件序；返回写入路径。
        禁止事项：不要追加写（同一 turn 重跑会留下两份矛盾的流水）。
        异常：OSError（磁盘/权限）。
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            for envelope in self._events:
                handle.write(envelope.to_json_line())
                handle.write("\n")
        return path


@dataclass(slots=True)
class Turn:
    """一次用户输入到一次交付的完整生命周期。

    前置条件：由 Turn.open 创建，不要直接构造（turn_id 生成规则集中在 open）。
    后置条件：turn.ledger 恒存在；state 只能按状态机推进。
    禁止事项：禁止跨 turn 共享 ledger。
    """

    turn_id: str
    user_text: str
    ledger: TurnLedger = field(init=False)
    state: TurnState = TurnState.INGEST

    def __post_init__(self) -> None:
        self.ledger = TurnLedger(self.turn_id)

    @classmethod
    def open(cls, user_text: str) -> "Turn":
        """开启一个新 turn。

        前置条件：user_text 可以为空字符串（校验交由上层 CLI）。
        后置条件：返回 state=INGEST 的新 Turn，turn_id 形如 t-YYYYMMDD-HHMMSS-abcd12。
        禁止事项：不要在没有用户输入时开 turn（会污染 ledger 与记忆）。
        异常：无。
        """
        turn_id = f"t-{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"
        return cls(turn_id=turn_id, user_text=user_text)
