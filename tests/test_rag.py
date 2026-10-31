"""ADR-038: parent expansion, FTS5 BM25, and hybrid RAG."""

import tempfile
import unittest
from pathlib import Path

from agent.adapters.sqlite_vectors import SqliteVectorStore
from agent.core.harness import _compact_answer_payload
from agent.core.protocol import ToolCall, ToolResult
from agent.knowledge.base import KnowledgeBase
from agent.knowledge.markdown import Chunker
from agent.ports import Chunk
from agent.tools.knowledge import SearchTool


class FakeEmbedder:
    """Deterministic character-bag vectors; no model calls."""

    def embed(self, texts):
        vectors = []
        for text in texts:
            vector = [0.0] * 16
            for char in text.casefold():
                if char.isalnum():
                    vector[ord(char) % 16] += 1.0
            vectors.append(vector)
        return vectors


class RagTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.docs = self.root / "docs"
        self.docs.mkdir()
        (self.docs / "alpha.md").write_text(
            "# Alpha\n\n## Step 1\n\nalpha first\n\n"
            "## Step 2\n\nalpha second\n\n## Step 3\n\nalpha third\n",
            encoding="utf-8",
        )
        (self.docs / "beta.md").write_text("# Beta\n\nbeta only\n", encoding="utf-8")
        self.store = SqliteVectorStore(self.root / "rag.db")
        self.knowledge = KnowledgeBase(
            embedder=FakeEmbedder(),
            store=self.store,
            chunker=Chunker(max_chars=100),
            candidate_k=10,
            parent_chars=3000,
            hybrid=True,
        )

    def test_section_parent_and_document_outline(self) -> None:
        self.knowledge.index(self.docs)
        hits = self.knowledge.search("alpha", 1)
        self.assertEqual(len(hits), 1)
        hit = hits[0]
        self.assertEqual(hit.chunk.doc_id, "alpha.md")
        self.assertIn("Step 1", hit.parent_text)
        self.assertNotIn("Step 3", hit.parent_text)
        self.assertEqual(hit.outline, ("Step 1",))
        self.assertEqual(hit.document_outline, ("Step 1", "Step 2", "Step 3"))
        self.assertGreater(hit.vector_score, 0.0)

    def test_procedure_query_expands_sibling_sections(self) -> None:
        self.knowledge.index(self.docs)
        hits = self.knowledge.search("alpha 步骤", 1)
        headings = {hit.chunk.heading for hit in hits}
        self.assertEqual(headings, {"Step 1", "Step 2", "Step 3"})
        self.assertTrue(all(hit.document_outline == ("Step 1", "Step 2", "Step 3") for hit in hits))

    def test_fts_bm25_finds_english_and_chinese(self) -> None:
        chunk = Chunk(
            chunk_id="c1",
            doc_id="refund.md",
            heading="退款",
            ordinal=0,
            text="退款期限是 30 天 refund policy",
            parent_id="refund.md",
        )
        self.store.replace_all([chunk], [[1.0] * 16])
        self.assertTrue(self.store.fts_enabled)
        self.assertEqual([item.chunk_id for item in self.store.bm25_search("refund", 5)], ["c1"])
        self.assertEqual([item.chunk_id for item in self.store.bm25_search("退款期限", 5)], ["c1"])

    def test_search_tool_includes_parent_and_outline(self) -> None:
        self.knowledge.index(self.docs)
        tool = SearchTool(knowledge=self.knowledge, default_k=1)
        result = tool.invoke(ToolCall(call_id="c-1", tool="knowledge.search", args={"query": "alpha", "k": 1}))
        self.assertTrue(result.ok)
        hits = result.data["hits"]
        self.assertEqual(len(hits), 1)
        self.assertIn("Step 1", hits[0]["text"])
        self.assertNotIn("Step 3", hits[0]["text"])
        self.assertEqual(hits[0]["outline"], ["Step 1"])
        self.assertEqual(hits[0]["document_outline"], ["Step 1", "Step 2", "Step 3"])
        self.assertIn("matched_text", hits[0])

    def test_exclude_globs_skips_worklog(self) -> None:
        (self.docs / "WORKLOG.md").write_text("# Worklog", encoding="utf-8")
        knowledge = KnowledgeBase(
            embedder=FakeEmbedder(),
            store=self.store,
            chunker=Chunker(max_chars=100),
            exclude_globs=("WORKLOG.md",),
        )
        knowledge.index(self.docs)
        self.assertNotIn("WORKLOG.md", knowledge.document_ids())

    def test_answer_payload_deduplicates_and_caps_parent_text(self) -> None:
        long_text = "x" * 5000
        results = [
            ToolResult(
                call_id="c-1",
                ok=True,
                data={"hits": [{"doc_id": "same.md", "heading": "A", "text": long_text}]},
            ),
            ToolResult(
                call_id="c-2",
                ok=True,
                data={"hits": [{"doc_id": "same.md", "heading": "A", "text": "duplicate"}]},
            ),
        ]
        results.append(ToolResult(
            call_id="c-3",
            ok=True,
            data={"hits": [{"doc_id": "same.md", "heading": "B", "text": "second section"}]},
        ))
        payload = _compact_answer_payload(results)
        hits = payload[0]["data"]["hits"]
        self.assertEqual(len(hits), 2)
        self.assertLessEqual(sum(len(hit["text"]) for hit in hits), 8000)


if __name__ == "__main__":
    unittest.main()
