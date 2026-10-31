"""base — 知识库：索引、父块扩展、hybrid 检索与按节回读。

Module: agent.knowledge.base
Responsibility: 组织 MarkdownLoader/Chunker/EmbedderPort/VectorStore，提供 index/search/read_section。
Depends on: stdlib (math, pathlib, re, typing) + agent.ports + agent.knowledge.markdown
Depended by: tools.knowledge, app
ADRs: ADR-016（索引与检索）, ADR-017（嵌入）, ADR-003（FP64）, ADR-038（父块与 hybrid）
"""

import fnmatch
import math
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from agent.knowledge.markdown import Chunker, MarkdownLoader
from agent.ports import Chunk, EmbedderPort, VectorStore

_TERM = re.compile(r"[0-9A-Za-z_\u4e00-\u9fff]+")
_PROCEDURE_QUERY = re.compile(r"(?:步骤|流程|有哪些|steps?|process|how\s+to|checklist)", re.IGNORECASE)
_MAX_ROLLUP_SECTIONS = 12


class KnowledgeError(RuntimeError):
    """知识库不可用（索引为空、维度不一致、文档不存在）。

    前置条件：无。
    后置条件：异常信息对用户可读（说明"下一步做什么"）。
    禁止事项：不要用它表达"检索分数低"（那是 score 的事）。
    异常来源：KnowledgeBase.index/search/read_section。
    """


@dataclass(frozen=True, slots=True)
class Hit:
    """一条检索命中。

    前置条件：score 为最终排序分；vector_score 为余弦相似度。
    后置条件：parent_text 为父文档/父节文本；outline 为该父块的标题列表。
    禁止事项：不要在 Hit 中改写 chunk 文本。
    """

    chunk: Chunk
    score: float
    parent_text: str = ""
    outline: tuple[str, ...] = ()
    document_outline: tuple[str, ...] = ()
    vector_score: float = 0.0
    lexical_score: float = 0.0


@dataclass(frozen=True, slots=True)
class IndexResult:
    """一次索引的结果。

    前置条件：由 KnowledgeBase.index 构造。
    后置条件：documents/chunks 为本次写入的数量；dim 为向量维度（空索引为 None）。
    禁止事项：不要把"读取到的文件数"与"写入的 chunk 数"混为一谈。
    """

    documents: int
    chunks: int
    dim: int | None


def cosine_fp64(left: Sequence[float], right: Sequence[float]) -> float:
    """计算两个向量的余弦相似度（双精度）。

    前置条件：两向量等长且非零；长度不一致视为编程错误。
    后置条件：返回 [-1, 1] 的 float；数值异常（NaN/inf）时返回 0.0。
    禁止事项：不要在此做归一化写回（调用方的向量必须保持原值）。
    异常：ValueError（长度不一致）。
    """
    if len(left) != len(right):
        raise ValueError(f"vector length mismatch: {len(left)} vs {len(right)}")
    dot = 0.0
    left_norm = 0.0
    right_norm = 0.0
    for a, b in zip(left, right):
        dot += float(a) * float(b)
        left_norm += float(a) * float(a)
        right_norm += float(b) * float(b)
    if left_norm <= 0.0 or right_norm <= 0.0:
        return 0.0
    score = dot / (math.sqrt(left_norm) * math.sqrt(right_norm))
    if not math.isfinite(score):
        return 0.0
    return min(max(score, -1.0), 1.0)


class KnowledgeBase:
    """本地 Markdown 知识库（ADR-016/038）。

    前置条件：embedder 与 store 均已就绪；chunker 的 max_chars 合理。
    后置条件：index 之后 search/read_section 立即反映新内容。
    禁止事项：禁止在索引过程中并发检索（同一 store 会被清空）。
    """

    def __init__(
        self,
        *,
        embedder: EmbedderPort,
        store: VectorStore,
        chunker: Chunker,
        loader: MarkdownLoader | None = None,
        batch_size: int = 16,
        candidate_k: int = 20,
        parent_chars: int = 2400,
        hybrid: bool = True,
        rrf_k: int = 60,
        exclude_globs: tuple[str, ...] = (),
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        if candidate_k < 1:
            raise ValueError("candidate_k must be >= 1")
        if parent_chars < 100:
            raise ValueError("parent_chars must be >= 100")
        if rrf_k < 1:
            raise ValueError("rrf_k must be >= 1")
        self._embedder = embedder
        self._store = store
        self._chunker = chunker
        self._loader = loader or MarkdownLoader()
        self._batch_size = batch_size
        self._candidate_k = candidate_k
        self._parent_chars = parent_chars
        self._hybrid = hybrid
        self._rrf_k = rrf_k
        self._exclude_globs = exclude_globs

    def index(self, root: Path) -> IndexResult:
        """全量重建索引。"""
        if not root.is_dir():
            raise KnowledgeError(f"knowledge root is not a directory: {root}")

        documents = 0
        chunks: list[Chunk] = []
        for path in sorted(root.rglob("*.md")):
            relative = path.relative_to(root)
            if any(part.startswith(".") for part in relative.parts):
                continue
            doc_id = relative.as_posix()
            if any(fnmatch.fnmatch(doc_id, pattern) for pattern in self._exclude_globs):
                continue
            document = self._loader.load(path, doc_id=doc_id)
            if not document.sections:
                continue
            documents += 1
            chunks.extend(self._chunker.split(document))

        if not chunks:
            raise KnowledgeError(f"no markdown content found under {root}")

        vectors: list[Sequence[float]] = []
        for start in range(0, len(chunks), self._batch_size):
            batch = chunks[start : start + self._batch_size]
            vectors.extend(self._embedder.embed([chunk.text for chunk in batch]))

        if len(vectors) != len(chunks):
            raise KnowledgeError(f"embedder returned {len(vectors)} vectors for {len(chunks)} chunks")
        dims = {len(vector) for vector in vectors}
        if len(dims) != 1:
            raise KnowledgeError(f"inconsistent embedding dimensions: {sorted(dims)}")

        self._store.replace_all(chunks, vectors)
        return IndexResult(documents=documents, chunks=len(chunks), dim=dims.pop())

    def search(self, query: str, k: int) -> list[Hit]:
        """检索 top-k 父块：向量 + BM25 融合、去重、启发式重排。"""
        if not query.strip():
            raise KnowledgeError("query must be non-empty")
        if k < 1:
            raise KnowledgeError("k must be >= 1")
        records = self._store.load_all()
        if not records:
            raise KnowledgeError("knowledge index is empty; call knowledge.index first")

        candidate_k = max(k, self._candidate_k)
        query_vector = self._embedder.embed([query])[0]
        vector_ranked = sorted(
            ((chunk, cosine_fp64(query_vector, vector)) for chunk, vector in records),
            key=lambda item: item[1],
            reverse=True,
        )[:candidate_k]

        fused: dict[str, float] = defaultdict(float)
        vector_scores: dict[str, float] = {}
        lexical_scores: dict[str, float] = {}
        chunk_by_id: dict[str, Chunk] = {chunk.chunk_id: chunk for chunk, _vector in records}
        for rank, (chunk, score) in enumerate(vector_ranked, start=1):
            fused[chunk.chunk_id] += 1.0 / (self._rrf_k + rank)
            vector_scores[chunk.chunk_id] = score

        bm25 = getattr(self._store, "bm25_search", None)
        if self._hybrid and callable(bm25):
            for rank, chunk in enumerate(bm25(query, candidate_k), start=1):
                chunk_by_id.setdefault(chunk.chunk_id, chunk)
                fused[chunk.chunk_id] += 1.0 / (self._rrf_k + rank)
                lexical_scores[chunk.chunk_id] = 1.0 / (self._rrf_k + rank)

        siblings: dict[str, list[Chunk]] = defaultdict(list)
        documents: dict[str, list[Chunk]] = defaultdict(list)
        for chunk, _vector in records:
            siblings[chunk.parent_id or chunk.doc_id].append(chunk)
            documents[chunk.doc_id].append(chunk)
        for group in siblings.values():
            group.sort(key=lambda item: item.ordinal)
        for group in documents.values():
            group.sort(key=lambda item: item.ordinal)
        doc_outlines = {doc_id: _outline(group) for doc_id, group in documents.items()}

        # 先按融合分挑每个父块的最佳 child，再扩展父块并做启发式重排。
        seen_parents: set[str] = set()
        expanded: list[Hit] = []
        for chunk_id, fused_score in sorted(fused.items(), key=lambda item: item[1], reverse=True):
            chunk = chunk_by_id.get(chunk_id)
            if chunk is None:
                continue
            parent_id = chunk.parent_id or chunk.doc_id
            if parent_id in seen_parents:
                continue
            seen_parents.add(parent_id)
            group = siblings.get(parent_id, [chunk])
            parent_text = _parent_text(chunk, group, self._parent_chars)
            outline = _outline(group)
            bonus = 0.02 * _char_coverage(query, parent_text)
            bonus += 0.01 * _char_coverage(query, chunk.heading)
            expanded.append(
                Hit(
                    chunk=chunk,
                    score=fused_score + bonus,
                    parent_text=parent_text,
                    outline=outline,
                    document_outline=doc_outlines.get(chunk.doc_id, ()),
                    vector_score=vector_scores.get(chunk_id, 0.0),
                    lexical_score=lexical_scores.get(chunk_id, 0.0),
                )
            )
        expanded.sort(key=lambda hit: hit.score, reverse=True)
        hits = expanded[:k]
        # 流程/步骤类问题：父块仍是 section；但把 top 文档的兄弟 section 一并带上，
        # 避免“命中门牌号却只拿到一个房间”导致步骤缺失。
        if _PROCEDURE_QUERY.search(query) and hits:
            top_hit = hits[0]
            top_doc = top_hit.chunk.doc_id
            existing_parents = {top_hit.chunk.parent_id or top_hit.chunk.doc_id}
            rollup: list[Hit] = []
            for parent_id, group in siblings.items():
                if len(rollup) >= _MAX_ROLLUP_SECTIONS:
                    break
                if not group or group[0].doc_id != top_doc or parent_id in existing_parents:
                    continue
                child = group[0]
                rollup.append(
                    Hit(
                        chunk=child,
                        score=max(0.0, top_hit.score - 0.0001),
                        parent_text=_parent_text(child, group, self._parent_chars),
                        outline=_outline(group),
                        document_outline=doc_outlines.get(top_doc, ()),
                        vector_score=0.0,
                        lexical_score=0.0,
                    )
                )
                existing_parents.add(parent_id)
            others = [hit for hit in hits[1:] if hit.chunk.doc_id != top_doc]
            return [top_hit] + rollup + others
        return hits

    def read_section(self, doc_id: str, heading: str = "") -> str:
        """按文档与标题前缀回读原文。"""
        records = self._store.load_all()
        matched = [
            chunk
            for chunk, _vector in records
            if chunk.doc_id == doc_id and (not heading or chunk.heading.startswith(heading))
        ]
        if not matched:
            raise KnowledgeError(f"no section found for doc_id={doc_id!r} heading={heading!r}")
        matched.sort(key=lambda chunk: chunk.ordinal)
        return "\n\n".join(chunk.text for chunk in matched)

    def document_ids(self) -> list[str]:
        """列出索引中的文档 id。"""
        return sorted({chunk.doc_id for chunk, _vector in self._store.load_all()})

    def count(self) -> int:
        """返回已索引 chunk 数。"""
        return self._store.count()


def _parent_text(matched: Chunk, siblings: list[Chunk], max_chars: int) -> str:
    """构造父块文本；超长时优先保留命中 chunk 前后的相邻 chunk。"""
    if sum(len(chunk.text) for chunk in siblings) + 2 * max(0, len(siblings) - 1) <= max_chars:
        return "\n\n".join(chunk.text for chunk in siblings)

    index = next((i for i, chunk in enumerate(siblings) if chunk.chunk_id == matched.chunk_id), 0)
    selected = [matched]
    used = len(matched.text)
    left, right = index - 1, index + 1
    while left >= 0 or right < len(siblings):
        candidates = []
        if right < len(siblings):
            candidates.append((right, siblings[right]))
        if left >= 0:
            candidates.append((left, siblings[left]))
        for position, chunk in candidates:
            extra = len(chunk.text) + 2
            if used + extra <= max_chars:
                selected.append(chunk)
                used += extra
                if position == left:
                    left -= 1
                else:
                    right += 1
                break
        else:
            break
    selected.sort(key=lambda chunk: chunk.ordinal)
    return "\n\n".join(chunk.text for chunk in selected)[:max_chars]


def _outline(siblings: list[Chunk]) -> tuple[str, ...]:
    """按原文顺序返回父块的唯一标题。"""
    headings: list[str] = []
    seen: set[str] = set()
    for chunk in siblings:
        if chunk.heading and chunk.heading not in seen:
            seen.add(chunk.heading)
            headings.append(chunk.heading)
    return tuple(headings)


def _char_coverage(query: str, text: str) -> float:
    """query 字符在文本中的覆盖率；用于轻量启发式 rerank。"""
    query_chars = {char.casefold() for char in query if char.isalnum()}
    if not query_chars:
        return 0.0
    text_chars = {char.casefold() for char in text}
    return len(query_chars & text_chars) / len(query_chars)
