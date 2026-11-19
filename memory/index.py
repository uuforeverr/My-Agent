from __future__ import annotations

import re
import sqlite3
import sys
import threading
import time
from array import array
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from hashlib import sha1
from math import sqrt
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from memory.embeddings import EmbeddingProvider


FTS_SCHEMA_VERSION = "3"
DEFAULT_USER_ID = "local"


@dataclass(frozen=True)
class MemoryRecord:
    id: str
    content: str
    source: str
    scope: str
    created_at: str
    updated_at: str
    path: str = ""
    start_line: int = 1
    end_line: int = 1
    user_id: str = DEFAULT_USER_ID
    search_text: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MemorySearchResult:
    record: MemoryRecord
    score: float
    bm25_rank: Optional[int] = None
    vector_rank: Optional[int] = None
    vector_backend: str = "disabled"


class MemoryIndex:
    """SQLite-backed per-user BM25 + embedding retrieval with RRF fusion."""

    def __init__(
        self,
        db_path: Path,
        *,
        embedder: Optional[EmbeddingProvider] = None,
        vector_backend: str = "auto",
        candidate_limit: int = 30,
        rrf_k: int = 60,
    ):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self._lock = threading.RLock()
        self._query_cache_lock = threading.RLock()
        self._query_vector_cache: OrderedDict[str, list[float]] = OrderedDict()
        self._search_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="memory-search")
        self.embedder = embedder
        self.vector_backend_requested = self._normalize_vector_backend(vector_backend)
        self.vector_backend = "disabled"
        self.vector_error = ""
        self.candidate_limit = max(1, int(candidate_limit))
        self.rrf_k = max(1, int(rrf_k))
        self.fts_available = self._init_db()
        self._init_vector_backend()

    def rebuild(
        self,
        files: list[tuple[Path, str] | tuple[Path, str, str]],
        root: Path,
    ) -> None:
        with self._lock:
            self._clear_locked()
            for item in files:
                path, source_type, user_id = self._file_item(item)
                self._index_file_locked(
                    path,
                    source_type=source_type,
                    root=root,
                    default_user_id=user_id,
                )

    def reindex_file(
        self,
        path: Path,
        source_type: str,
        root: Path,
        user_id: str = DEFAULT_USER_ID,
    ) -> None:
        with self._lock:
            rel_path = path.relative_to(root).as_posix()
            self._delete_source_locked(rel_path)
            self._index_file_locked(
                path,
                source_type=source_type,
                root=root,
                default_user_id=user_id,
            )

    def remove_source(self, rel_path: str) -> None:
        with self._lock:
            self._delete_source_locked(rel_path)

    def list_source_paths(self) -> list[str]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT DISTINCT source_path FROM memory_chunks",
            ).fetchall()
            return [row["source_path"] for row in rows]

    def clear(self) -> None:
        with self._lock:
            self._clear_locked()

    def index_file(
        self,
        path: Path,
        source_type: str,
        root: Path,
        user_id: str = DEFAULT_USER_ID,
    ) -> None:
        with self._lock:
            self._index_file_locked(
                path,
                source_type=source_type,
                root=root,
                default_user_id=user_id,
            )

    def upsert_record(self, record: MemoryRecord) -> None:
        """Index one canonical SQLite-backed memory record."""
        with self._lock:
            text = str(record.search_text or record.content or "").strip()
            if not text:
                return
            user_id = self._normalize_user_id(record.user_id)
            text_hash = sha1(text.encode("utf-8")).hexdigest()
            self.conn.execute(
                """
                INSERT INTO memory_chunks(
                    id, user_id, source_type, source_path, start_line, end_line,
                    text, hash, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    user_id=excluded.user_id,
                    source_type=excluded.source_type,
                    source_path=excluded.source_path,
                    start_line=excluded.start_line,
                    end_line=excluded.end_line,
                    text=excluded.text,
                    hash=excluded.hash,
                    updated_at=excluded.updated_at
                """,
                (
                    record.id, user_id, record.source, record.path,
                    int(record.start_line), int(record.end_line), text, text_hash,
                    record.created_at, record.updated_at,
                ),
            )
            self._upsert_fts(record.id, user_id, text)
            if self.embedder is not None and self.embedder.available:
                vectors = self.embedder.encode([text])
                if vectors:
                    self._upsert_embedding(record.id, user_id, vectors[0])
            self.conn.commit()

    def remove_record(self, record_id: str) -> None:
        with self._lock:
            row = self.conn.execute(
                "SELECT rowid FROM memory_chunks WHERE id = ?", (record_id,)
            ).fetchone()
            if row is not None and self.vector_backend == "sqlite-vec":
                self.conn.execute("DELETE FROM memory_chunks_vec WHERE rowid = ?", (row["rowid"],))
            self.conn.execute("DELETE FROM memory_embeddings WHERE chunk_id = ?", (record_id,))
            if self.fts_available:
                self.conn.execute("DELETE FROM memory_chunks_fts WHERE id = ?", (record_id,))
            self.conn.execute("DELETE FROM memory_chunks WHERE id = ?", (record_id,))
            self.conn.commit()

    def search(
        self,
        query: str,
        limit: int = 5,
        min_score: float = 0.05,
        *,
        user_id: str = DEFAULT_USER_ID,
    ) -> list[MemorySearchResult]:
        results, _metadata = self.search_with_metadata(
            query,
            limit=limit,
            min_score=min_score,
            user_id=user_id,
        )
        return results

    def search_with_metadata(
        self,
        query: str,
        limit: int = 5,
        min_score: float = 0.05,
        *,
        user_id: str = DEFAULT_USER_ID,
    ) -> tuple[list[MemorySearchResult], dict[str, Any]]:
        started = time.perf_counter()
        safe_user_id = self._normalize_user_id(user_id)
        text = str(query or "").strip()
        if limit <= 0 or not text:
            return [], {
                "user_id": safe_user_id,
                "bm25_candidates": 0,
                "vector_candidates": 0,
                "vector_backend": self.vector_backend,
                "duration_ms": 0.0,
            }

        candidate_limit = max(self.candidate_limit, int(limit) * 4)
        bm25_future = self._search_executor.submit(
            self._timed_bm25,
            text,
            safe_user_id,
            candidate_limit,
        )
        vector_future = None
        if self.embedder is not None and self.embedder.available:
            vector_future = self._search_executor.submit(self._timed_query_vector, text)

        bm25_rows, bm25_ms = bm25_future.result()
        query_vector: list[float] = []
        embedding_ms = 0.0
        if vector_future is not None:
            query_vector, embedding_ms = vector_future.result()

        vector_started = time.perf_counter()
        vector_rows: list[sqlite3.Row] = []
        backend_used = "disabled"
        if query_vector:
            vector_rows, backend_used = self._vector_ranked(
                query_vector,
                safe_user_id,
                candidate_limit,
            )
        vector_ms = (time.perf_counter() - vector_started) * 1000

        fused = self._rrf_fuse(
            bm25_rows,
            vector_rows,
            limit=int(limit),
            min_score=float(min_score),
            vector_backend=backend_used,
        )
        metadata = {
            "user_id": safe_user_id,
            "bm25_candidates": len(bm25_rows),
            "vector_candidates": len(vector_rows),
            "bm25_duration_ms": round(bm25_ms, 2),
            "embedding_duration_ms": round(embedding_ms, 2),
            "vector_duration_ms": round(vector_ms, 2),
            "vector_backend": backend_used,
            "rrf_k": self.rrf_k,
            "result_count": len(fused),
            "result_ids": [item.record.id for item in fused],
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        }
        return fused, metadata

    def list_recent(
        self,
        limit: int = 80,
        *,
        user_id: str = DEFAULT_USER_ID,
    ) -> list[MemoryRecord]:
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT id, user_id, source_type, source_path, start_line, end_line,
                       text, created_at, updated_at
                FROM memory_chunks
                WHERE user_id = ?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (self._normalize_user_id(user_id), limit),
            ).fetchall()
        return [self._record_from_row(row) for row in rows]

    def close(self) -> None:
        self._search_executor.shutdown(wait=True)
        with self._lock:
            self.conn.close()

    def _init_db(self) -> bool:
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS memory_chunks (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL DEFAULT 'local',
                source_type TEXT NOT NULL,
                source_path TEXT NOT NULL,
                start_line INTEGER NOT NULL,
                end_line INTEGER NOT NULL,
                text TEXT NOT NULL,
                hash TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        columns = {
            row["name"] for row in self.conn.execute("PRAGMA table_info(memory_chunks)").fetchall()
        }
        if "user_id" not in columns:
            self.conn.execute(
                "ALTER TABLE memory_chunks ADD COLUMN user_id TEXT NOT NULL DEFAULT 'local'"
            )
        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_memory_chunks_user_source
            ON memory_chunks(user_id, source_type, source_path)
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS memory_embeddings (
                chunk_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                model_id TEXT NOT NULL,
                dimensions INTEGER NOT NULL,
                embedding BLOB NOT NULL,
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY(chunk_id) REFERENCES memory_chunks(id) ON DELETE CASCADE
            )
        """)
        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_memory_embeddings_user_model
            ON memory_embeddings(user_id, model_id)
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS memory_index_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        self.conn.commit()

        try:
            schema_row = self.conn.execute(
                "SELECT value FROM memory_index_meta WHERE key = 'fts_schema_version'"
            ).fetchone()
            current_version = str(schema_row["value"]) if schema_row is not None else ""
            fts_columns = {
                row["name"]
                for row in self.conn.execute("PRAGMA table_info(memory_chunks_fts)").fetchall()
            }
            if current_version != FTS_SCHEMA_VERSION or fts_columns != {"id", "user_id", "terms"}:
                self.conn.execute("DROP TABLE IF EXISTS memory_chunks_fts")
            self.conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_chunks_fts
                USING fts5(id UNINDEXED, user_id UNINDEXED, terms, tokenize='unicode61')
            """)
            self.conn.execute(
                """
                INSERT INTO memory_index_meta(key, value) VALUES('fts_schema_version', ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (FTS_SCHEMA_VERSION,),
            )
            self.conn.commit()
            return True
        except sqlite3.OperationalError:
            return False

    def _init_vector_backend(self) -> None:
        if self.embedder is None or not self.embedder.available or self.embedder.dimensions <= 0:
            self.vector_backend = "disabled"
            return
        if self.vector_backend_requested == "python":
            self.vector_backend = "python"
            return
        try:
            import sqlite_vec

            self.conn.enable_load_extension(True)
            sqlite_vec.load(self.conn)
            dimensions = int(self.embedder.dimensions)
            schema_key = f"{self.embedder.model_id}:{dimensions}"
            current = self.conn.execute(
                "SELECT value FROM memory_index_meta WHERE key = 'vec_schema'"
            ).fetchone()
            if current is None or current["value"] != schema_key:
                self.conn.execute("DROP TABLE IF EXISTS memory_chunks_vec")
            self.conn.execute(
                f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_chunks_vec USING vec0(
                    user_id TEXT partition key,
                    embedding float[{dimensions}]
                )
                """
            )
            self.conn.execute(
                """
                INSERT INTO memory_index_meta(key, value) VALUES('vec_schema', ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (schema_key,),
            )
            self.conn.commit()
            self.vector_backend = "sqlite-vec"
        except Exception as exc:
            self.vector_error = f"{type(exc).__name__}: {exc}"
            self.vector_backend = "python"

    def _clear_locked(self) -> None:
        if self.vector_backend == "sqlite-vec":
            self.conn.execute("DELETE FROM memory_chunks_vec")
        self.conn.execute("DELETE FROM memory_embeddings")
        self.conn.execute("DELETE FROM memory_chunks")
        if self.fts_available:
            self.conn.execute("DELETE FROM memory_chunks_fts")
        self.conn.commit()

    def _delete_source_locked(self, rel_path: str) -> None:
        rows = self.conn.execute(
            "SELECT rowid, id FROM memory_chunks WHERE source_path = ?",
            (rel_path,),
        ).fetchall()
        if self.vector_backend == "sqlite-vec":
            self.conn.executemany(
                "DELETE FROM memory_chunks_vec WHERE rowid = ?",
                [(row["rowid"],) for row in rows],
            )
        self.conn.executemany(
            "DELETE FROM memory_embeddings WHERE chunk_id = ?",
            [(row["id"],) for row in rows],
        )
        if self.fts_available:
            self.conn.executemany(
                "DELETE FROM memory_chunks_fts WHERE id = ?",
                [(row["id"],) for row in rows],
            )
        self.conn.execute(
            "DELETE FROM memory_chunks WHERE source_path = ?",
            (rel_path,),
        )
        self.conn.commit()

    def _index_file_locked(
        self,
        path: Path,
        source_type: str,
        root: Path,
        default_user_id: str,
    ) -> None:
        if not path.exists() or not path.is_file():
            return

        content = path.read_text(encoding="utf-8").strip()
        if not content:
            return

        rel_path = path.relative_to(root).as_posix()
        pending_embeddings: list[tuple[str, str, str]] = []
        for start_line, end_line, text in self._chunk_markdown(content):
            user_id = self._extract_user_id(text) or self._normalize_user_id(default_user_id)
            chunk_id = self._make_chunk_id(rel_path, start_line, end_line, text, user_id)
            text_hash = sha1(text.encode("utf-8")).hexdigest()
            self.conn.execute(
                """
                INSERT OR REPLACE INTO memory_chunks
                    (id, user_id, source_type, source_path, start_line, end_line,
                     text, hash, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                """,
                (
                    chunk_id,
                    user_id,
                    source_type,
                    rel_path,
                    start_line,
                    end_line,
                    text,
                    text_hash,
                ),
            )
            self._upsert_fts(chunk_id, user_id, text)
            pending_embeddings.append((chunk_id, user_id, text))

        if self.embedder is not None and self.embedder.available and pending_embeddings:
            vectors = self.embedder.encode([item[2] for item in pending_embeddings])
            for item, vector in zip(pending_embeddings, vectors):
                self._upsert_embedding(item[0], item[1], vector)
        self.conn.commit()

    def _upsert_embedding(self, chunk_id: str, user_id: str, vector: Sequence[float]) -> None:
        if not vector:
            return
        payload = self._pack_vector(vector)
        self.conn.execute(
            """
            INSERT INTO memory_embeddings(
                chunk_id, user_id, model_id, dimensions, embedding, updated_at
            ) VALUES (?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(chunk_id) DO UPDATE SET
                user_id=excluded.user_id,
                model_id=excluded.model_id,
                dimensions=excluded.dimensions,
                embedding=excluded.embedding,
                updated_at=excluded.updated_at
            """,
            (
                chunk_id,
                user_id,
                self.embedder.model_id if self.embedder is not None else "",
                len(vector),
                payload,
            ),
        )
        if self.vector_backend == "sqlite-vec":
            row = self.conn.execute(
                "SELECT rowid FROM memory_chunks WHERE id = ?",
                (chunk_id,),
            ).fetchone()
            if row is not None:
                self.conn.execute("DELETE FROM memory_chunks_vec WHERE rowid = ?", (row["rowid"],))
                self.conn.execute(
                    "INSERT INTO memory_chunks_vec(rowid, user_id, embedding) VALUES (?, ?, ?)",
                    (row["rowid"], user_id, payload),
                )

    def _timed_bm25(
        self,
        query: str,
        user_id: str,
        limit: int,
    ) -> tuple[list[sqlite3.Row], float]:
        started = time.perf_counter()
        rows = self._bm25_ranked(query, user_id, limit)
        return rows, (time.perf_counter() - started) * 1000

    def _bm25_ranked(self, query: str, user_id: str, limit: int) -> list[sqlite3.Row]:
        if not self.fts_available:
            return self._fallback_lexical_ranked(query, user_id, limit)
        terms = self._fts_terms(query)
        if not terms:
            return []
        fts_query = " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms[:24])
        try:
            with self._lock:
                return self.conn.execute(
                    """
                    SELECT c.id, c.user_id, c.source_type, c.source_path,
                           c.start_line, c.end_line, c.text, c.created_at,
                           c.updated_at, bm25(memory_chunks_fts) AS bm25_score
                    FROM memory_chunks_fts
                    JOIN memory_chunks AS c ON c.id = memory_chunks_fts.id
                    WHERE memory_chunks_fts MATCH ?
                      AND memory_chunks_fts.user_id = ?
                    ORDER BY bm25_score ASC
                    LIMIT ?
                    """,
                    (fts_query, user_id, limit),
                ).fetchall()
        except sqlite3.OperationalError:
            return self._fallback_lexical_ranked(query, user_id, limit)

    def _fallback_lexical_ranked(
        self,
        query: str,
        user_id: str,
        limit: int,
    ) -> list[sqlite3.Row]:
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT id, user_id, source_type, source_path, start_line, end_line,
                       text, created_at, updated_at
                FROM memory_chunks
                WHERE user_id = ?
                """,
                (user_id,),
            ).fetchall()
        query_terms = set(self._fts_terms(query))
        query_text = query.strip().lower()

        def score(row: sqlite3.Row) -> tuple[int, int, str]:
            text = row["text"].lower()
            row_terms = set(self._fts_terms(text))
            hits = len(query_terms.intersection(row_terms))
            exact = 1 if query_text and query_text in text else 0
            return exact, hits, row["updated_at"]

        matched = [row for row in rows if score(row)[:2] != (0, 0)]
        matched.sort(key=score, reverse=True)
        return matched[:limit]

    def _timed_query_vector(self, query: str) -> tuple[list[float], float]:
        started = time.perf_counter()
        vector = self._query_vector(query)
        return vector, (time.perf_counter() - started) * 1000

    def _query_vector(self, query: str) -> list[float]:
        cache_key = f"{self.embedder.model_id if self.embedder else ''}:{query.strip()}"
        with self._query_cache_lock:
            cached = self._query_vector_cache.get(cache_key)
            if cached is not None:
                self._query_vector_cache.move_to_end(cache_key)
                return list(cached)
        if self.embedder is None or not self.embedder.available:
            return []
        rows = self.embedder.encode([query])
        vector = rows[0] if rows else []
        if vector:
            with self._query_cache_lock:
                self._query_vector_cache[cache_key] = list(vector)
                self._query_vector_cache.move_to_end(cache_key)
                while len(self._query_vector_cache) > 128:
                    self._query_vector_cache.popitem(last=False)
        return vector

    def _vector_ranked(
        self,
        query_vector: Sequence[float],
        user_id: str,
        limit: int,
    ) -> tuple[list[sqlite3.Row], str]:
        if self.vector_backend == "sqlite-vec":
            try:
                return self._sqlite_vector_ranked(query_vector, user_id, limit), "sqlite-vec"
            except Exception as exc:
                self.vector_error = f"{type(exc).__name__}: {exc}"
        return self._python_vector_ranked(query_vector, user_id, limit), "python"

    def _sqlite_vector_ranked(
        self,
        query_vector: Sequence[float],
        user_id: str,
        limit: int,
    ) -> list[sqlite3.Row]:
        payload = self._pack_vector(query_vector)
        with self._lock:
            matches = self.conn.execute(
                """
                SELECT rowid, distance
                FROM memory_chunks_vec
                WHERE user_id = ? AND embedding MATCH ? AND k = ?
                ORDER BY distance
                """,
                (user_id, payload, limit),
            ).fetchall()
            if not matches:
                return []
            by_rowid = {int(row["rowid"]): float(row["distance"]) for row in matches}
            placeholders = ",".join("?" for _ in by_rowid)
            rows = self.conn.execute(
                f"""
                SELECT rowid AS chunk_rowid, id, user_id, source_type, source_path,
                       start_line, end_line, text, created_at, updated_at
                FROM memory_chunks
                WHERE rowid IN ({placeholders}) AND user_id = ?
                """,
                [*by_rowid.keys(), user_id],
            ).fetchall()
        return sorted(rows, key=lambda row: by_rowid[int(row["chunk_rowid"])])

    def _python_vector_ranked(
        self,
        query_vector: Sequence[float],
        user_id: str,
        limit: int,
    ) -> list[sqlite3.Row]:
        if self.embedder is None:
            return []
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT c.id, c.user_id, c.source_type, c.source_path,
                       c.start_line, c.end_line, c.text, c.created_at,
                       c.updated_at, e.embedding
                FROM memory_embeddings AS e
                JOIN memory_chunks AS c ON c.id = e.chunk_id
                WHERE e.user_id = ? AND e.model_id = ? AND e.dimensions = ?
                """,
                (user_id, self.embedder.model_id, len(query_vector)),
            ).fetchall()
        query_norm = sqrt(sum(value * value for value in query_vector)) or 1.0

        def similarity(row: sqlite3.Row) -> float:
            vector = self._unpack_vector(row["embedding"])
            if len(vector) != len(query_vector):
                return float("-inf")
            norm = sqrt(sum(value * value for value in vector)) or 1.0
            return sum(a * b for a, b in zip(query_vector, vector)) / (query_norm * norm)

        ranked = sorted(rows, key=similarity, reverse=True)
        return ranked[:limit]

    def _rrf_fuse(
        self,
        bm25_rows: Sequence[sqlite3.Row],
        vector_rows: Sequence[sqlite3.Row],
        *,
        limit: int,
        min_score: float,
        vector_backend: str,
    ) -> list[MemorySearchResult]:
        ranks: dict[str, dict[str, Any]] = {}
        for rank, row in enumerate(bm25_rows, 1):
            ranks.setdefault(row["id"], {"row": row})["bm25_rank"] = rank
        for rank, row in enumerate(vector_rows, 1):
            ranks.setdefault(row["id"], {"row": row})["vector_rank"] = rank

        max_rrf = 2.0 / (self.rrf_k + 1)
        results: list[MemorySearchResult] = []
        for item in ranks.values():
            bm25_rank = item.get("bm25_rank")
            vector_rank = item.get("vector_rank")
            raw_score = 0.0
            if bm25_rank is not None:
                raw_score += 1.0 / (self.rrf_k + bm25_rank)
            if vector_rank is not None:
                raw_score += 1.0 / (self.rrf_k + vector_rank)
            score = raw_score / max_rrf
            if score < min_score:
                continue
            results.append(
                MemorySearchResult(
                    record=self._record_from_row(item["row"]),
                    score=score,
                    bm25_rank=bm25_rank,
                    vector_rank=vector_rank,
                    vector_backend=vector_backend,
                )
            )
        results.sort(
            key=lambda result: (result.score, result.record.updated_at),
            reverse=True,
        )
        return results[:limit]

    def _upsert_fts(self, chunk_id: str, user_id: str, text: str) -> None:
        if not self.fts_available:
            return
        self.conn.execute("DELETE FROM memory_chunks_fts WHERE id = ?", (chunk_id,))
        self.conn.execute(
            "INSERT INTO memory_chunks_fts (id, user_id, terms) VALUES (?, ?, ?)",
            (chunk_id, user_id, " ".join(self._fts_terms(text))),
        )

    def _record_from_row(self, row: sqlite3.Row) -> MemoryRecord:
        keys = set(row.keys())
        return MemoryRecord(
            id=row["id"],
            content=row["text"],
            source=row["source_type"],
            scope="user",
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            path=row["source_path"],
            start_line=row["start_line"],
            end_line=row["end_line"],
            user_id=row["user_id"] if "user_id" in keys else DEFAULT_USER_ID,
        )

    def _chunk_markdown(self, content: str) -> list[tuple[int, int, str]]:
        lines = content.splitlines()
        chunks = []
        current = []
        start_line = 1

        for index, line in enumerate(lines, 1):
            if line.strip().startswith("## ") and current:
                chunks.append((start_line, index - 1, "\n".join(current).strip()))
                current = [line]
                start_line = index
            else:
                if not current:
                    start_line = index
                current.append(line)

        if current:
            text = "\n".join(current).strip()
            if text:
                chunks.append((start_line, len(lines), text))
        return chunks

    @classmethod
    def _fts_terms(cls, text: str) -> list[str]:
        lowered = str(text or "").lower()
        terms: list[str] = []
        for part in re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]+", lowered):
            if re.fullmatch(r"[\u4e00-\u9fff]+", part):
                if len(part) == 1:
                    terms.append(part)
                else:
                    terms.extend(part[index : index + 2] for index in range(len(part) - 1))
                    if len(part) <= 8:
                        terms.append(part)
            else:
                terms.append(part)
        return list(dict.fromkeys(term for term in terms if term.strip()))

    @staticmethod
    def _extract_user_id(text: str) -> str:
        match = re.search(r"(?mi)^\s*user_id\s*:\s*`?([^`\s]+)", text or "")
        return match.group(1).strip() if match else ""

    @staticmethod
    def _normalize_user_id(user_id: str) -> str:
        return str(user_id or DEFAULT_USER_ID).strip() or DEFAULT_USER_ID

    @staticmethod
    def _normalize_vector_backend(raw: str) -> str:
        value = str(raw or "auto").strip().lower().replace("_", "-")
        return value if value in {"auto", "sqlite-vec", "python"} else "auto"

    @staticmethod
    def _pack_vector(vector: Sequence[float]) -> bytes:
        values = array("f", (float(value) for value in vector))
        if sys.byteorder != "little":
            values.byteswap()
        return values.tobytes()

    @staticmethod
    def _unpack_vector(payload: bytes) -> list[float]:
        values = array("f")
        values.frombytes(bytes(payload))
        if sys.byteorder != "little":
            values.byteswap()
        return [float(value) for value in values]

    @classmethod
    def _make_chunk_id(
        cls,
        path: str,
        start_line: int,
        end_line: int,
        text: str,
        user_id: str = DEFAULT_USER_ID,
    ) -> str:
        raw = (
            f"{cls._normalize_user_id(user_id)}:{path}:{start_line}:{end_line}:"
            f"{sha1(text.encode('utf-8')).hexdigest()}"
        )
        return "chunk_" + sha1(raw.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _file_item(
        item: tuple[Path, str] | tuple[Path, str, str],
    ) -> tuple[Path, str, str]:
        if len(item) == 3:
            return item[0], item[1], str(item[2] or DEFAULT_USER_ID)
        return item[0], item[1], DEFAULT_USER_ID
