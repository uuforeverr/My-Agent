import re
import sqlite3
import threading
from dataclasses import dataclass
from hashlib import sha1
from pathlib import Path


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


@dataclass(frozen=True)
class MemorySearchResult:
    record: MemoryRecord
    score: float


class MemoryIndex:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self.fts_available = self._init_db()

    def rebuild(self, files: list[tuple[Path, str]], root: Path) -> None:
        with self._lock:
            self._clear_locked()
            for path, source_type in files:
                self._index_file_locked(path, source_type=source_type, root=root)

    def reindex_file(self, path: Path, source_type: str, root: Path) -> None:
        with self._lock:
            rel_path = str(path.relative_to(root))
            self._delete_source_locked(rel_path)
            self._index_file_locked(path, source_type=source_type, root=root)

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

    def index_file(self, path: Path, source_type: str, root: Path) -> None:
        with self._lock:
            self._index_file_locked(path, source_type=source_type, root=root)

    def _clear_locked(self) -> None:
        self.conn.execute("DELETE FROM memory_chunks")
        if self.fts_available:
            self.conn.execute("DELETE FROM memory_chunks_fts")
        self.conn.commit()

    def _delete_source_locked(self, rel_path: str) -> None:
        if self.fts_available:
            self.conn.execute(
                """
                DELETE FROM memory_chunks_fts
                WHERE id IN (
                    SELECT id FROM memory_chunks WHERE source_path = ?
                )
                """,
                (rel_path,),
            )
        self.conn.execute(
            "DELETE FROM memory_chunks WHERE source_path = ?",
            (rel_path,),
        )
        self.conn.commit()

    def _index_file_locked(self, path: Path, source_type: str, root: Path) -> None:
        if not path.exists() or not path.is_file():
            return

        content = path.read_text(encoding="utf-8").strip()
        if not content:
            return

        rel_path = str(path.relative_to(root))
        for start_line, end_line, text in self._chunk_markdown(content):
            chunk_id = self._make_chunk_id(rel_path, start_line, end_line, text)
            text_hash = sha1(text.encode("utf-8")).hexdigest()
            self.conn.execute(
                """
                INSERT OR REPLACE INTO memory_chunks
                    (id, source_type, source_path, start_line, end_line, text, hash, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
                """,
                (chunk_id, source_type, rel_path, start_line, end_line, text, text_hash),
            )
            self._upsert_fts(chunk_id, text)
        self.conn.commit()

    def search(
        self,
        query: str,
        limit: int = 5,
        min_score: float = 0.05,
    ) -> list[MemorySearchResult]:
        if limit <= 0:
            return []

        with self._lock:
            candidates = self._candidate_rows(query, max(limit * 8, 20))
        results = []
        for row in candidates:
            score = self._score(query, row["text"], row["source_type"])
            if score >= min_score:
                results.append(MemorySearchResult(
                    record=self._record_from_row(row),
                    score=score,
                ))
        results.sort(key=lambda item: (item.score, item.record.updated_at), reverse=True)
        return results[:limit]

    def list_recent(self, limit: int = 80) -> list[MemoryRecord]:
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT id, source_type, source_path, start_line, end_line, text, created_at, updated_at
                FROM memory_chunks
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._record_from_row(row) for row in rows]

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def _init_db(self) -> bool:
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS memory_chunks (
                id TEXT PRIMARY KEY,
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
        self.conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_memory_chunks_source
            ON memory_chunks(source_type, source_path)
        """)
        self.conn.commit()

        try:
            self.conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_chunks_fts
                USING fts5(id UNINDEXED, text)
            """)
            self.conn.commit()
            return True
        except sqlite3.OperationalError:
            return False

    def _candidate_rows(self, query: str, limit: int) -> list[sqlite3.Row]:
        ids = self._search_fts_ids(query, limit)
        rows = []
        if ids:
            placeholders = ",".join("?" for _ in ids)
            rows = self.conn.execute(
                f"""
                SELECT id, source_type, source_path, start_line, end_line, text, created_at, updated_at
                FROM memory_chunks
                WHERE id IN ({placeholders})
                """,
                ids,
            ).fetchall()

        if len(rows) < limit:
            seen = {row["id"] for row in rows}
            fallback = self.conn.execute(
                """
                SELECT id, source_type, source_path, start_line, end_line, text, created_at, updated_at
                FROM memory_chunks
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            rows.extend(row for row in fallback if row["id"] not in seen)

        return rows[:limit]

    def _search_fts_ids(self, query: str, limit: int) -> list[str]:
        if not self.fts_available:
            return []

        tokens = self._tokens(query)
        if not tokens:
            return []

        fts_query = " OR ".join(f'"{token}"' for token in tokens[:8])
        try:
            rows = self.conn.execute(
                """
                SELECT id
                FROM memory_chunks_fts
                WHERE memory_chunks_fts MATCH ?
                LIMIT ?
                """,
                (fts_query, limit),
            ).fetchall()
            return [row["id"] for row in rows]
        except sqlite3.OperationalError:
            return []

    def _upsert_fts(self, chunk_id: str, text: str) -> None:
        if not self.fts_available:
            return
        self.conn.execute("DELETE FROM memory_chunks_fts WHERE id = ?", (chunk_id,))
        self.conn.execute(
            "INSERT INTO memory_chunks_fts (id, text) VALUES (?, ?)",
            (chunk_id, text),
        )

    def _record_from_row(self, row: sqlite3.Row) -> MemoryRecord:
        return MemoryRecord(
            id=row["id"],
            content=row["text"],
            source=row["source_type"],
            scope="shared",
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            path=row["source_path"],
            start_line=row["start_line"],
            end_line=row["end_line"],
        )

    def _score(self, query: str, text: str, source_type: str) -> float:
        query_text = query.strip().lower()
        content_text = text.lower()
        tokens = set(self._tokens(query_text))
        if not query_text and not tokens:
            return 0.1

        score = 0.0
        if query_text and query_text in content_text:
            score += 0.6
        if tokens:
            hits = sum(1 for token in tokens if token in content_text)
            score += hits / len(tokens)
        if source_type == "semantic":
            score += 0.15
        elif source_type == "episodic":
            score += 0.05
        return min(score, 1.0)

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

    @staticmethod
    def _tokens(text: str) -> list[str]:
        lowered = text.lower()
        words = re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]", lowered)
        return [word for word in words if word.strip()]

    @staticmethod
    def _make_chunk_id(path: str, start_line: int, end_line: int, text: str) -> str:
        raw = f"{path}:{start_line}:{end_line}:{sha1(text.encode('utf-8')).hexdigest()}"
        return "chunk_" + sha1(raw.encode("utf-8")).hexdigest()[:16]
