"""RAG retrieval baseline: Recall@k, MRR, and heading coverage.

Run from the project root after rebuilding the index:

    PYTHONPATH=. python3 test_history/rag_golden/eval_rag.py
"""

import argparse
import json
import sys
from pathlib import Path

from agent.adapters.embeddings import LlamaCppEmbedder
from agent.adapters.llama_cpp import ServerSupervisor
from agent.adapters.sqlite_vectors import SqliteVectorStore
from agent.config import load
from agent.knowledge.base import KnowledgeBase
from agent.knowledge.markdown import Chunker

ROOT = Path(__file__).resolve().parents[2]
QUERIES = Path(__file__).with_name("queries.jsonl")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--k", type=int, default=5)
    args = parser.parse_args()
    if args.k < 1:
        print("k must be >= 1", file=sys.stderr)
        return 2

    cfg = load()
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    supervisor = ServerSupervisor(
        binary=cfg.llama_binary,
        profiles=cfg.models,
        lock_path=cfg.lock_path,
        log_dir=cfg.log_dir,
        health_timeout_s=cfg.health_timeout_s,
    )
    knowledge = KnowledgeBase(
        embedder=LlamaCppEmbedder(
            supervisor=supervisor,
            alias=cfg.knowledge.embed_alias,
            timeout_s=cfg.brain_timeout_s,
        ),
        store=SqliteVectorStore(cfg.knowledge.db_path),
        chunker=Chunker(max_chars=cfg.knowledge.chunk_chars),
        candidate_k=cfg.knowledge.candidate_k,
        parent_chars=cfg.knowledge.parent_chars,
        hybrid=cfg.knowledge.hybrid,
        rrf_k=cfg.knowledge.rrf_k,
        exclude_globs=cfg.knowledge.exclude_globs,
    )

    rows = [json.loads(line) for line in QUERIES.read_text(encoding="utf-8").splitlines() if line.strip()]
    recalls = {1: [], 3: [], 5: []}
    reciprocal_ranks: list[float] = []
    coverage: list[float] = []
    failures: list[str] = []
    try:
        for item in rows:
            hits = knowledge.search(item["query"], args.k)
            docs = [hit.chunk.doc_id for hit in hits]
            print(f"\n[{item['id']}] {item['query']}")
            for index, hit in enumerate(hits, start=1):
                print(f"  {index}. {hit.score:.4f} {hit.chunk.doc_id} :: {hit.chunk.heading}")
            if not item.get("answerable", True):
                print("  unanswerable: check the final answer refuses instead of inventing")
                continue
            expected = set(item.get("expected_docs", []))
            rank = next((i for i, doc in enumerate(docs, start=1) if doc in expected), None)
            for cutoff in recalls:
                recalls[cutoff].append(1.0 if rank is not None and rank <= cutoff else 0.0)
            reciprocal_ranks.append(1.0 / rank if rank else 0.0)
            if rank is None:
                failures.append(item["id"])
            expected_headings = item.get("expected_headings", [])
            if expected_headings:
                outlines = {
                    heading
                    for hit in hits
                    if hit.chunk.doc_id in expected
                    for heading in hit.document_outline
                }
                present = sum(1 for heading in expected_headings if heading in outlines)
                coverage.append(present / len(expected_headings))
    finally:
        supervisor.close()

    print("\n== Summary ==")
    for cutoff in recalls:
        values = recalls[cutoff]
        print(f"Recall@{cutoff}: {sum(values) / len(values):.3f}" if values else f"Recall@{cutoff}: n/a")
    print(f"MRR: {sum(reciprocal_ranks) / len(reciprocal_ranks):.3f}" if reciprocal_ranks else "MRR: n/a")
    print(f"Outline coverage: {sum(coverage) / len(coverage):.3f}" if coverage else "Outline coverage: n/a")
    if failures:
        print("Misses: " + ", ".join(failures))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
