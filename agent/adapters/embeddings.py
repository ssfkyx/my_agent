"""embeddings — llama-server 的嵌入实现（EmbedderPort）。

Module: agent.adapters.embeddings
Responsibility: 通过 /v1/embeddings 把文本批量转成 FP64 向量；负责 ensure 嵌入模型驻留。
Depends on: stdlib (json, urllib, array, time) + agent.ports + agent.adapters.llama_cpp(ServerSupervisor)
Depended by: app（装配）, knowledge.base（索引/检索）
ADRs: ADR-017（embed role 与互斥）, ADR-002（单常驻）, ADR-003（FP64）
"""

import json
import urllib.error
import urllib.request
from array import array
from typing import Sequence

from agent.adapters.llama_cpp import ServerSupervisor
from agent.ports import EmbeddingError


class LlamaCppEmbedder:
    """用常驻的 embed 模型做向量化（ADR-017）。

    前置条件：supervisor 的 profiles 含 alias（默认 embed）；服务以 --embeddings 启动。
    后置条件：每次 embed 都返回与输入等长的 FP64 向量。
    禁止事项：不要在 embed 内并发调用（会与对话模型抢显存）。
    """

    def __init__(self, *, supervisor: ServerSupervisor, alias: str, timeout_s: float) -> None:
        self._supervisor = supervisor
        self._alias = alias
        self._timeout_s = timeout_s

    def embed(self, texts: Sequence[str]) -> list[Sequence[float]]:
        """批量向量化。

        前置条件：texts 非空、每条非空；服务可由 supervisor 拉起。
        后置条件：返回与 texts 等长的 array('d') 列表，维度一致。
        禁止事项：不要在此截断文本（索引与查询必须同源）。
        异常：EmbeddingError（服务不可用、HTTP 非 2xx、维度不一致）。
        """
        if not texts:
            raise EmbeddingError("embed() requires at least one text")
        if any(not text.strip() for text in texts):
            raise EmbeddingError("embed() received an empty text")

        base_url = self._supervisor.ensure(self._alias)
        body = {"model": self._alias, "input": list(texts)}
        request = urllib.request.Request(
            f"{base_url}/v1/embeddings",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_s) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise EmbeddingError(f"HTTP {exc.code} from /v1/embeddings: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise EmbeddingError(f"cannot reach {base_url}/v1/embeddings: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise EmbeddingError("embedding response is not JSON") from exc

        data = payload.get("data")
        if not isinstance(data, list) or len(data) != len(texts):
            raise EmbeddingError(f"embedding response size mismatch: {len(data) if isinstance(data, list) else 'n/a'}")

        vectors: list[Sequence[float]] = []
        dims = set()
        for item in data:
            raw = item.get("embedding") if isinstance(item, dict) else None
            if not isinstance(raw, list) or not raw:
                raise EmbeddingError("embedding response item is malformed")
            vector = array("d", (float(value) for value in raw))
            dims.add(len(vector))
            vectors.append(vector)
        if len(dims) != 1:
            raise EmbeddingError(f"inconsistent embedding dimensions: {sorted(dims)}")
        return vectors
