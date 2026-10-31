"""knowledge — 知识库工具：index / reindex / search / read_section / list_documents。

Module: agent.tools.knowledge
Responsibility: 把 KnowledgeBase 暴露成主 agent 可调用的工具，并把领域异常翻译成 ToolResult。
Depends on: stdlib (time, pathlib) + agent.core.protocol + agent.knowledge.base + agent.ports
Depended by: app（注册）, core.harness（调用）
ADRs: ADR-016（检索语义）, ADR-017（嵌入）, ADR-009（index 的 path 走同一沙箱）, ADR-038（父块与 hybrid）
"""

import time
from pathlib import Path

from agent.core.protocol import ErrorCode, ErrorInfo, ToolCall, ToolResult, ToolSpec
from agent.knowledge.base import IndexResult, KnowledgeBase, KnowledgeError
from agent.ports import EmbeddingError, Tool

# 检索片段即证据：太短会逼模型再取原文，太长会压垮上下文（ADR-016 实测调优）。
_PREVIEW_CHARS = 800


class IndexTool:
    """重建知识库索引（显式路径）。

    前置条件：args.path 已由护栏归一化为 workspace 内的绝对目录。
    后置条件：索引内容等于该目录下全部 .md 的 chunk。
    禁止事项：不要对同一路径并发索引（会互相清空）。
    """

    spec = ToolSpec(
        name="knowledge.index",
        category="knowledge",
        description=(
            "Rebuild the local knowledge index from a directory of Markdown files. "
            "Run it once before searching, and again whenever the documents change."
        ),
        args_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory containing .md files; relative paths resolve against the workspace root.",
                }
            },
            "required": ["path"],
        },
        side_effect=True,
    )

    def __init__(self, *, knowledge: KnowledgeBase) -> None:
        self._knowledge = knowledge

    def invoke(self, call: ToolCall) -> ToolResult:
        """执行索引。

        前置条件：路径已归一化。
        后置条件：ok=True 时 data 含 documents/chunks/dim。
        禁止事项：不要在失败时留下"部分索引"（KnowledgeBase 用全量替换保证）。
        异常：无。
        """
        started = time.perf_counter()
        raw_path = call.args.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            return _fail(call, ErrorCode.BAD_ARGS, "args.path must be a non-empty string", started)
        root = Path(raw_path)
        if not root.is_absolute():
            return _fail(call, ErrorCode.BAD_ARGS, "args.path must be normalised to an absolute path", started)
        return _run_index(knowledge=self._knowledge, call=call, root=root, started=started)


class ReindexTool:
    """用默认 docs_root（或显式 path）重建索引。

    前置条件：default_root 为配置中的绝对目录；args.path 若给出则已归一化。
    后置条件：与 knowledge.index 完全相同的索引结果；省略 path 时使用配置默认值。
    禁止事项：不要在这里猜测 docs 根目录（默认值由 App 从配置注入）。
    """

    spec = ToolSpec(
        name="knowledge.reindex",
        category="knowledge",
        description=(
            "Rebuild the knowledge index from the configured docs root (or a given path). "
            "Use it after Markdown documents change without having to know the absolute root."
        ),
        args_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Optional directory; relative paths resolve against the workspace root.",
                }
            },
        },
        side_effect=True,
    )

    def __init__(self, *, knowledge: KnowledgeBase, default_root: Path) -> None:
        if not default_root.is_absolute():
            raise ValueError("default_root must be an absolute path")
        self._knowledge = knowledge
        self._default_root = default_root

    def invoke(self, call: ToolCall) -> ToolResult:
        """执行重建。

        前置条件：path 缺省时使用构造时注入的 docs_root。
        后置条件：ok=True 时 data 含 root/documents/chunks/dim。
        禁止事项：不要把缺省路径解析成进程 cwd（必须用配置值）。
        异常：无。
        """
        started = time.perf_counter()
        raw_path = call.args.get("path")
        if raw_path is None:
            root = self._default_root
        else:
            if not isinstance(raw_path, str) or not raw_path.strip():
                return _fail(call, ErrorCode.BAD_ARGS, "args.path must be a non-empty string", started)
            root = Path(raw_path)
            if not root.is_absolute():
                return _fail(call, ErrorCode.BAD_ARGS, "args.path must be normalised to an absolute path", started)
        return _run_index(knowledge=self._knowledge, call=call, root=root, started=started)


def _run_index(*, knowledge: KnowledgeBase, call: ToolCall, root: Path, started: float) -> ToolResult:
    """执行一次全量索引并翻译异常。

    前置条件：root 为绝对路径；started 为 perf_counter 起点。
    后置条件：成功返回 root/documents/chunks/dim；失败返回对应错误码。
    禁止事项：不要在失败时返回部分索引结果。
    异常：无。
    """
    try:
        result: IndexResult = knowledge.index(root)
    except KnowledgeError as exc:
        return _fail(call, ErrorCode.NOT_FOUND, str(exc), started)
    except EmbeddingError as exc:
        return _fail(call, ErrorCode.UPSTREAM_ERROR, f"embedding failed: {exc}", started, retryable=True)
    except OSError as exc:
        return _fail(call, ErrorCode.INTERNAL, f"index failed: {exc}", started)

    return ToolResult(
        call_id=call.call_id,
        ok=True,
        data={
            "root": str(root),
            "documents": result.documents,
            "chunks": result.chunks,
            "dim": result.dim,
        },
        elapsed_s=time.perf_counter() - started,
    )


class SearchTool:
    """语义检索 top-k 片段。

    前置条件：索引已建立（否则返回 NOT_FOUND 并提示先 index）。
    后置条件：hits 按 score 降序，每条含 doc_id/heading/score/text 预览。
    禁止事项：不要把 score 当"概率"解释（它只是余弦相似度）。
    """

    spec = ToolSpec(
        name="knowledge.search",
        category="knowledge",
        description=(
            "Search indexed Markdown documents by meaning and return the most relevant parent sections "
            "with doc_id, heading, section text, and document outline. Use it to find procedure files before planning. "
            "For Chinese requests, run a second query in English (and vice versa)."
        ),
        args_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to look for, in natural language."},
                "k": {"type": "integer", "description": "How many passages to return (default from config)."},
            },
            "required": ["query"],
        },
        side_effect=False,
    )

    def __init__(self, *, knowledge: KnowledgeBase, default_k: int) -> None:
        self._knowledge = knowledge
        self._default_k = default_k

    def invoke(self, call: ToolCall) -> ToolResult:
        """执行检索。

        前置条件：query 非空；k 缺省时使用配置值。
        后置条件：data.hits 为列表（可能为空但不会缺键）。
        禁止事项：不要在空索引上伪造命中。
        异常：无。
        """
        started = time.perf_counter()
        query = call.args.get("query")
        if not isinstance(query, str) or not query.strip():
            return _fail(call, ErrorCode.BAD_ARGS, "args.query must be a non-empty string", started)
        raw_k = call.args.get("k", self._default_k)
        if not isinstance(raw_k, int) or isinstance(raw_k, bool):
            return _fail(call, ErrorCode.BAD_ARGS, "args.k must be an integer", started)
        k = min(max(raw_k, 1), 20)

        try:
            hits = self._knowledge.search(query, k)
        except KnowledgeError as exc:
            return _fail(call, ErrorCode.NOT_FOUND, str(exc), started, hint="call knowledge.index first")
        except EmbeddingError as exc:
            return _fail(call, ErrorCode.UPSTREAM_ERROR, f"embedding failed: {exc}", started, retryable=True)
        except OSError as exc:
            return _fail(call, ErrorCode.INTERNAL, f"search failed: {exc}", started)

        return ToolResult(
            call_id=call.call_id,
            ok=True,
            data={
                "query": query,
                "k": k,
                "hits": [
                    {
                        "doc_id": hit.chunk.doc_id,
                        "heading": hit.chunk.heading,
                        "score": hit.score,
                        "vector_score": hit.vector_score,
                        "lexical_score": hit.lexical_score,
                        "text": hit.parent_text or _preview(hit.chunk.text),
                        "matched_text": _preview(hit.chunk.text),
                        "outline": list(hit.outline),
                        "document_outline": list(hit.document_outline),
                    }
                    for hit in hits
                ],
            },
            elapsed_s=time.perf_counter() - started,
        )


class ReadSectionTool:
    """按文档与标题前缀回读原文。

    前置条件：doc_id 来自 search/list_documents。
    后置条件：返回完整小节文本（未截断）。
    禁止事项：不要把返回文本再摘要后再交给调用方（调用方需要原文）。
    """

    spec = ToolSpec(
        name="knowledge.read_section",
        category="knowledge",
        description=(
            "Read the full text of one section (or a whole document when heading is empty) "
            "from the knowledge index, identified by doc_id returned from search."
        ),
        args_schema={
            "type": "object",
            "properties": {
                "doc_id": {"type": "string", "description": "Document id as returned by knowledge.search."},
                "heading": {"type": "string", "description": "Optional heading prefix to narrow the section."},
            },
            "required": ["doc_id"],
        },
        side_effect=False,
    )

    def __init__(self, *, knowledge: KnowledgeBase) -> None:
        self._knowledge = knowledge

    def invoke(self, call: ToolCall) -> ToolResult:
        """回读小节。

        前置条件：doc_id 非空。
        后置条件：data.text 为原文；找不到时 NOT_FOUND。
        禁止事项：不要为空结果返回空字符串冒充成功。
        异常：无。
        """
        started = time.perf_counter()
        doc_id = call.args.get("doc_id")
        if not isinstance(doc_id, str) or not doc_id.strip():
            return _fail(call, ErrorCode.BAD_ARGS, "args.doc_id must be a non-empty string", started)
        heading = call.args.get("heading", "")
        if not isinstance(heading, str):
            return _fail(call, ErrorCode.BAD_ARGS, "args.heading must be a string", started)

        try:
            text = self._knowledge.read_section(doc_id, heading)
        except KnowledgeError as exc:
            return _fail(call, ErrorCode.NOT_FOUND, str(exc), started)
        except OSError as exc:
            return _fail(call, ErrorCode.INTERNAL, f"read failed: {exc}", started)

        return ToolResult(
            call_id=call.call_id,
            ok=True,
            data={"doc_id": doc_id, "heading": heading, "text": text},
            elapsed_s=time.perf_counter() - started,
        )


class ListDocumentsTool:
    """列出索引中的文档。

    前置条件：无。
    后置条件：data.documents 为去重后的相对路径列表。
    禁止事项：不要返回 chunk 粒度的 id。
    """

    spec = ToolSpec(
        name="knowledge.list_documents",
        category="knowledge",
        description="List all document ids currently present in the knowledge index.",
        args_schema={"type": "object", "properties": {}, "required": []},
        side_effect=False,
    )

    def __init__(self, *, knowledge: KnowledgeBase) -> None:
        self._knowledge = knowledge

    def invoke(self, call: ToolCall) -> ToolResult:
        """列出文档。

        前置条件：无。
        后置条件：ok=True 时 data.documents 为列表（可能为空）。
        禁止事项：不要在空索引时报错（返回空列表即可）。
        异常：无。
        """
        started = time.perf_counter()
        try:
            documents = self._knowledge.document_ids()
        except OSError as exc:
            return _fail(call, ErrorCode.INTERNAL, f"list failed: {exc}", started)
        return ToolResult(
            call_id=call.call_id,
            ok=True,
            data={"documents": documents, "chunks": self._knowledge.count()},
            elapsed_s=time.perf_counter() - started,
        )


def _preview(text: str) -> str:
    """截断检索片段用于回执。

    前置条件：text 为非空字符串。
    后置条件：长度不超过 _PREVIEW_CHARS。
    禁止事项：不要把截断标记省略（调用方需要知道还有后续）。
    异常：无。
    """
    if len(text) <= _PREVIEW_CHARS:
        return text
    return text[: _PREVIEW_CHARS - 1] + "…"


def _fail(
    call: ToolCall,
    code: ErrorCode,
    message: str,
    started: float,
    *,
    retryable: bool = False,
    hint: str = "",
) -> ToolResult:
    """构造失败回执。

    前置条件：started 为 perf_counter 起点。
    后置条件：返回 ok=False 且 call_id 对齐的 ToolResult。
    禁止事项：不要吞掉 call_id。
    异常：无。
    """
    return ToolResult(
        call_id=call.call_id,
        ok=False,
        error=ErrorInfo(code=code, message=message, retryable=retryable, hint=hint),
        elapsed_s=time.perf_counter() - started,
    )


def build_tools(
    knowledge: KnowledgeBase,
    *,
    default_k: int,
    default_docs_root: Path | None = None,
) -> list[Tool]:
    """构造知识类工具列表。

    前置条件：knowledge 已就绪；default_docs_root 若给出则为绝对路径。
    后置条件：返回 [IndexTool, (ReindexTool), SearchTool, ReadSectionTool, ListDocumentsTool]。
    禁止事项：不要在工具内部再构造 KnowledgeBase（共享同一索引）。
    异常：无。
    """
    tools: list[Tool] = [IndexTool(knowledge=knowledge)]
    if default_docs_root is not None:
        tools.append(ReindexTool(knowledge=knowledge, default_root=default_docs_root))
    tools.extend(
        [
            SearchTool(knowledge=knowledge, default_k=default_k),
            ReadSectionTool(knowledge=knowledge),
            ListDocumentsTool(knowledge=knowledge),
        ]
    )
    return tools
