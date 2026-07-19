import json
import sqlite3
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any, Optional

from observability.tracing import TraceQuery, redact_data

DB_SCHEMA_VERSION = 2


def default_trace_db_path(project_root: Path) -> Path:
    return project_root / "data" / "observability" / "traces.db"


class SqliteTraceRepository:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._init_db()

    @classmethod
    def for_project(cls, project_root: Path) -> "SqliteTraceRepository":
        return cls(default_trace_db_path(project_root))

    def append_record(self, record: dict[str, Any]) -> None:
        redacted = redact_data(record)
        trace_id = str(redacted.get("trace_id") or redacted.get("request_id") or "")
        if not trace_id:
            return
        root_span = self._root_span(redacted)
        root_attributes = root_span.get("attributes") or {}
        record_json = json.dumps(redacted, ensure_ascii=False, default=str)

        with self._lock, self._connect() as conn:
            conn.execute("BEGIN")
            conn.execute(
                """
                INSERT OR REPLACE INTO traces (
                    trace_id, request_id, schema_version, created_at, ended_at,
                    created_at_ts, status, total_ms, root_span_id, root_name, session_id,
                    entrypoint, record_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trace_id,
                    redacted.get("request_id"),
                    redacted.get("schema_version"),
                    redacted.get("created_at"),
                    redacted.get("ended_at"),
                    _parse_iso_ts(redacted.get("created_at")),
                    redacted.get("status"),
                    redacted.get("total_ms"),
                    redacted.get("root_span_id"),
                    root_span.get("name"),
                    root_attributes.get("session_id"),
                    root_attributes.get("entrypoint") or root_attributes.get("channel_type"),
                    record_json,
                ),
            )
            conn.execute("DELETE FROM spans WHERE trace_id = ?", (trace_id,))
            for span in redacted.get("spans") or []:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO spans (
                        span_id, trace_id, parent_span_id, name, span_kind,
                        status, duration_ms, created_at, ended_at, attributes_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        span.get("span_id"),
                        trace_id,
                        span.get("parent_span_id"),
                        span.get("name"),
                        span.get("span_kind"),
                        span.get("status"),
                        span.get("duration_ms"),
                        span.get("created_at"),
                        span.get("ended_at"),
                        json.dumps(span.get("attributes") or {}, ensure_ascii=False, default=str),
                    ),
                )
            conn.commit()

    def read_records(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT record_json FROM traces ORDER BY id ASC"
            ).fetchall()
        return [redact_data(json.loads(row["record_json"])) for row in rows]

    def list_recent(
        self,
        limit: int = 10,
        status: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        return self.query_records(TraceQuery(limit=limit, status=status))

    def query_records(self, query: TraceQuery) -> list[dict[str, Any]]:
        where_clauses: list[str] = []
        params: list[Any] = []
        if query.status:
            where_clauses.append("status = ?")
            params.append(query.status)
        if query.session_id:
            where_clauses.append("session_id = ?")
            params.append(query.session_id)
        if query.entrypoint:
            where_clauses.append("entrypoint = ?")
            params.append(query.entrypoint)
        if query.start:
            where_clauses.append("created_at >= ?")
            params.append(query.start)
        if query.end:
            where_clauses.append("created_at <= ?")
            params.append(query.end)
        if query.before:
            before_id = self._row_id_for_cursor(query.before)
            if before_id is None:
                return []
            where_clauses.append("id < ?")
            params.append(before_id)
        where = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        params.append(query.safe_limit)
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                f"SELECT record_json FROM traces {where} ORDER BY id DESC LIMIT ?",
                params,
            ).fetchall()
        return [redact_data(json.loads(row["record_json"])) for row in rows]

    def get(self, trace_id: str) -> Optional[dict[str, Any]]:
        wanted = (trace_id or "").strip()
        if not wanted:
            return None
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT record_json FROM traces
                WHERE trace_id = ? OR request_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (wanted, wanted),
            ).fetchone()
        if row is None:
            return None
        return redact_data(json.loads(row["record_json"]))

    def rebuild_from_records(self, records: list[dict[str, Any]]) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM spans")
            conn.execute("DELETE FROM traces")
            conn.commit()
        for record in records:
            self.append_record(record)

    def trace_ids(self) -> set[str]:
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT trace_id FROM traces").fetchall()
        return {str(row["trace_id"]) for row in rows}

    def _connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock:
            with self._connect() as conn:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS traces (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        trace_id TEXT NOT NULL UNIQUE,
                        request_id TEXT,
                        schema_version TEXT,
                        created_at TEXT,
                        ended_at TEXT,
                        created_at_ts REAL,
                        status TEXT,
                        total_ms REAL,
                        root_span_id TEXT,
                        root_name TEXT,
                        session_id TEXT,
                        entrypoint TEXT,
                        record_json TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_traces_created_at ON traces(created_at);
                    CREATE INDEX IF NOT EXISTS idx_traces_created_at_ts ON traces(created_at_ts);
                    CREATE INDEX IF NOT EXISTS idx_traces_status ON traces(status);
                    CREATE INDEX IF NOT EXISTS idx_traces_session_id ON traces(session_id);
                    CREATE INDEX IF NOT EXISTS idx_traces_entrypoint ON traces(entrypoint);

                    CREATE TABLE IF NOT EXISTS spans (
                        span_id TEXT PRIMARY KEY,
                        trace_id TEXT NOT NULL,
                        parent_span_id TEXT,
                        name TEXT,
                        span_kind TEXT,
                        status TEXT,
                        duration_ms REAL,
                        created_at TEXT,
                        ended_at TEXT,
                        attributes_json TEXT,
                        FOREIGN KEY(trace_id) REFERENCES traces(trace_id) ON DELETE CASCADE
                    );
                    CREATE INDEX IF NOT EXISTS idx_spans_trace_id ON spans(trace_id);
                    CREATE INDEX IF NOT EXISTS idx_spans_kind ON spans(span_kind);
                    CREATE INDEX IF NOT EXISTS idx_spans_status ON spans(status);
                    """
                )
                self._migrate(conn)

    def _migrate(self, conn) -> None:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(traces)").fetchall()}
        if "created_at_ts" not in columns:
            conn.execute("ALTER TABLE traces ADD COLUMN created_at_ts REAL")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_traces_created_at_ts ON traces(created_at_ts)")
        rows = conn.execute(
            "SELECT id, created_at FROM traces WHERE created_at_ts IS NULL AND created_at IS NOT NULL"
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE traces SET created_at_ts = ? WHERE id = ?",
                (_parse_iso_ts(row["created_at"]), row["id"]),
            )
        conn.execute(f"PRAGMA user_version = {DB_SCHEMA_VERSION}")

    def _row_id_for_cursor(self, cursor: str) -> Optional[int]:
        wanted = (cursor or "").strip()
        if not wanted:
            return None
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT id FROM traces
                WHERE trace_id = ? OR request_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (wanted, wanted),
            ).fetchone()
        return int(row["id"]) if row is not None else None

    @staticmethod
    def _root_span(record: dict[str, Any]) -> dict[str, Any]:
        spans = record.get("spans") or []
        root_span_id = record.get("root_span_id")
        for span in spans:
            if span.get("span_id") == root_span_id:
                return span
        return spans[0] if spans else {}


def _parse_iso_ts(value: Any) -> Optional[float]:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None
