"""sqlite_vectors — SQLite 向量存储 + FTS5 词法索引（FP64 BLOB）。

Module: agent.adapters.sqlite_vectors
Responsibility: 实现 VectorStore：全量替换索引、读回全部记录与向量，并提供 BM25 候选。
Depends on: stdlib (sqlite3, array, pathlib, re) + agent.ports
Depended by: app（装配）, knowledge.base（检索）
ADRs: ADR-016（FP64 存储与全量重建）, ADR-003（FP64）, ADR-038（父块与 hybrid RAG）
"""

import re
import sqlite3
from array import array
from pathlib import Path
from typing import Sequence

from agent.ports import Chunk

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id TEXT PRIMARY KEY,
    doc_id   TEXT NOT NULL,
    heading  TEXT NOT NULL,
    ordinal  INTEGER NOT NULL,
    text     TEXT NOT NULL,
    parent_id TEXT NOT NULL DEFAULT '',
    dim      INTEGER NOT NULL,
    vector   BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);
"""

_TERM = re.compile(r"[0-9A-Za-z_\u4e00-\u9fff]+")


class SqliteVectorStore:
    """基于 SQLite 的向量存储 + FTS5 trigram 词法索引。

    前置条件：db_path 的父目录可写（由 App 创建）。
    后置条件：replace_all 后库内容与入参完全一致；向量逐位无损（FP64）。
    禁止事项：不要用 float32 或 JSON 存向量（精度与体积都会退化）。
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(_SCHEMA)
            self._migrate_parent_column(connection)
            connection.execute("CREATE INDEX IF NOT EXISTS idx_chunks_parent ON chunks(parent_id)")
            self._fts_enabled = self._init_fts(connection)
            if self._fts_enabled:
                self._rebuild_fts_if_needed(connection)

    @property
    def fts_enabled(self) -> bool:
        return self._fts_enabled

    def replace_all(self, chunks: Sequence[Chunk], vectors: Sequence[Sequence[float]]) -> None:
        """清空并写入全部 chunk、向量与 FTS 索引。

        前置条件：chunks 与 vectors 等长；每条向量等长。
        后置条件：事务提交后索引即为新内容；失败则回滚（旧索引保留）。
        禁止事项：不要在事务外逐条提交（中途失败会留下半个索引）。
        异常：ValueError（长度不一致）、sqlite3.Error。
        """
        if len(chunks) != len(vectors):
            raise ValueError(f"chunks/vectors mismatch: {len(chunks)} vs {len(vectors)}")
        rows = []
        fts_rows = []
        for chunk, vector in zip(chunks, vectors):
            payload = array("d", (float(value) for value in vector))
            parent_id = chunk.parent_id or chunk.doc_id
            rows.append(
                (
                    chunk.chunk_id,
                    chunk.doc_id,
                    chunk.heading,
                    chunk.ordinal,
                    chunk.text,
                    parent_id,
                    len(payload),
                    payload.tobytes(),
                )
            )
            fts_rows.append((chunk.chunk_id, chunk.doc_id, chunk.heading, chunk.text))
        with self._connect() as connection:
            connection.execute("DELETE FROM chunks")
            if self._fts_enabled:
                connection.execute("DELETE FROM chunks_fts")
            connection.executemany(
                "INSERT INTO chunks (chunk_id, doc_id, heading, ordinal, text, parent_id, dim, vector)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            if self._fts_enabled:
                connection.executemany(
                    "INSERT INTO chunks_fts (chunk_id, doc_id, heading, text) VALUES (?, ?, ?, ?)",
                    fts_rows,
                )

    def load_all(self) -> list[tuple[Chunk, Sequence[float]]]:
        """读回全部记录与向量。

        前置条件：无。
        后置条件：返回列表；向量为 array('d')。
        禁止事项：不要在读取时做解码/改写（检索层只做点积）。
        异常：sqlite3.Error。
        """
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT chunk_id, doc_id, heading, ordinal, text, parent_id, vector"
                " FROM chunks ORDER BY doc_id, ordinal"
            ).fetchall()
        records: list[tuple[Chunk, Sequence[float]]] = []
        for chunk_id, doc_id, heading, ordinal, text, parent_id, blob in rows:
            vector = array("d")
            vector.frombytes(blob)
            records.append(
                (
                    Chunk(
                        chunk_id=chunk_id,
                        doc_id=doc_id,
                        heading=heading,
                        ordinal=int(ordinal),
                        text=text,
                        parent_id=parent_id or doc_id,
                    ),
                    vector,
                )
            )
        return records

    def bm25_search(self, query: str, limit: int) -> list[Chunk]:
        """用 FTS5/BM25 返回词法候选；失败或短词时回退 LIKE。"""
        if limit < 1:
            return []
        terms = [term for term in _TERM.findall(query.casefold()) if len(term) >= 3]
        if not self._fts_enabled or not terms:
            return self._like_search(query, limit)
        match = " OR ".join(f'"{term}"' for term in terms)
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT c.chunk_id, c.doc_id, c.heading, c.ordinal, c.text, c.parent_id"
                    " FROM chunks_fts JOIN chunks c ON c.chunk_id = chunks_fts.chunk_id"
                    " WHERE chunks_fts MATCH ?"
                    " ORDER BY bm25(chunks_fts) LIMIT ?",
                    (match, limit),
                ).fetchall()
        except sqlite3.OperationalError:
            return self._like_search(query, limit)
        return [
            Chunk(
                chunk_id=chunk_id,
                doc_id=doc_id,
                heading=heading,
                ordinal=int(ordinal),
                text=text,
                parent_id=parent_id or doc_id,
            )
            for chunk_id, doc_id, heading, ordinal, text, parent_id in rows
        ]

    def count(self) -> int:
        """返回 chunk 数。"""
        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) FROM chunks").fetchone()
        return int(row[0]) if row else 0

    def dimension(self) -> int | None:
        """返回已存向量维度。"""
        with self._connect() as connection:
            row = connection.execute("SELECT dim FROM chunks LIMIT 1").fetchone()
        return int(row[0]) if row else None

    def _like_search(self, query: str, limit: int) -> list[Chunk]:
        needle = f"%{query.strip()}%"
        if not query.strip():
            return []
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT chunk_id, doc_id, heading, ordinal, text, parent_id"
                " FROM chunks WHERE text LIKE ? OR heading LIKE ?"
                " ORDER BY doc_id, ordinal LIMIT ?",
                (needle, needle, limit),
            ).fetchall()
        return [
            Chunk(
                chunk_id=chunk_id,
                doc_id=doc_id,
                heading=heading,
                ordinal=int(ordinal),
                text=text,
                parent_id=parent_id or doc_id,
            )
            for chunk_id, doc_id, heading, ordinal, text, parent_id in rows
        ]

    def _migrate_parent_column(self, connection: sqlite3.Connection) -> None:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(chunks)")}
        if "parent_id" not in columns:
            connection.execute("ALTER TABLE chunks ADD COLUMN parent_id TEXT NOT NULL DEFAULT ''")

    def _init_fts(self, connection: sqlite3.Connection) -> bool:
        existing = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'chunks_fts'"
        ).fetchone()
        if existing:
            return True
        for tokenizer in ("trigram", "unicode61"):
            try:
                connection.execute(
                    "CREATE VIRTUAL TABLE chunks_fts USING fts5("
                    "chunk_id UNINDEXED, doc_id UNINDEXED, heading, text, "
                    f"tokenize='{tokenizer}')"
                )
                return True
            except sqlite3.OperationalError:
                continue
        return False

    def _rebuild_fts_if_needed(self, connection: sqlite3.Connection) -> None:
        chunk_count = int(connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
        fts_count = int(connection.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0])
        if chunk_count == fts_count:
            return
        connection.execute("DELETE FROM chunks_fts")
        connection.execute(
            "INSERT INTO chunks_fts (chunk_id, doc_id, heading, text)"
            " SELECT chunk_id, doc_id, heading, text FROM chunks"
        )

    def _connect(self) -> sqlite3.Connection:
        """打开一个连接（自动提交由 with 块管理）。"""
        connection = sqlite3.connect(self._db_path)
        connection.execute("PRAGMA journal_mode=WAL")
        return connection
