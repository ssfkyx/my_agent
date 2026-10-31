"""memory — 只追加的 JSONL 长期记忆与工具变更队列。

Module: agent.adapters.memory
Responsibility: 实现 MemoryPort：追加反思、读取最近条目、记录工具变更请求。
Depends on: stdlib (json, pathlib, collections.deque)
Depended by: app（装配）, core.harness（注入 lessons）, tools.memory
ADRs: ADR-018（只追加）, ADR-003（FP64 时间戳）
"""

import json
from collections import deque
from pathlib import Path
from typing import Any, Mapping


class JsonlMemoryStore:
    """基于 JSONL 的长期记忆。

    前置条件：父目录可写（由 App 创建）。
    后置条件：append 之后 recent 立即可读；文件只增不改。
    禁止事项：不要提供"清空/重写"接口（需要清理时必须由人决定）。
    """

    def __init__(self, *, path: Path, requests_path: Path) -> None:
        self._path = path
        self._requests_path = requests_path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._requests_path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, entry: Mapping[str, Any]) -> None:
        """追加一条记录。

        前置条件：entry 可 JSON 序列化。
        后置条件：写入一行；失败则文件保持原样（append 语义）。
        禁止事项：不要先读后写整文件（会破坏并发追加）。
        异常：OSError（磁盘/权限）、TypeError（不可序列化）。
        """
        _append_line(self._path, entry)

    def recent(self, limit: int) -> list[Mapping[str, Any]]:
        """读取最近 limit 条（按时间升序）。

        前置条件：limit >= 0。
        后置条件：返回列表；文件不存在或为空时返回 []；损坏行被跳过。
        禁止事项：不要因单行损坏而整体失败（记忆是 best-effort）。
        异常：OSError。
        """
        if limit <= 0 or not self._path.exists():
            return []
        tail: deque[str] = deque(maxlen=limit)
        with self._path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    tail.append(line)
        entries: list[Mapping[str, Any]] = []
        for line in tail:
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, Mapping):
                entries.append(parsed)
        return entries

    def count(self) -> int:
        """返回记录条数。

        前置条件：无。
        后置条件：文件不存在时返回 0。
        禁止事项：不要为计数把整文件读进内存（流式计数）。
        异常：OSError。
        """
        if not self._path.exists():
            return 0
        with self._path.open("r", encoding="utf-8") as handle:
            return sum(1 for line in handle if line.strip())

    def record_tool_request(self, payload: Mapping[str, Any]) -> None:
        """把工具变更请求追加进队列文件。

        前置条件：payload 含 tool 与 reason。
        后置条件：队列文件多一行；工具注册表不受影响。
        禁止事项：不要在此调用工具或修改配置。
        异常：OSError、TypeError。
        """
        _append_line(self._requests_path, payload)


def _append_line(path: Path, payload: Mapping[str, Any]) -> None:
    """向 JSONL 文件追加一行。

    前置条件：payload 可 JSON 序列化。
    后置条件：追加成功则文件行数 +1；异常时文件不留下半行。
    禁止事项：不要用 "w" 模式打开（会截断历史）。
    异常：OSError、TypeError。
    """
    line = json.dumps(dict(payload), ensure_ascii=False) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
