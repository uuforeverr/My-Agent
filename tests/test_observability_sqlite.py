import json
import sqlite3

from observability import JsonlTraceStore, REDACTED, SqliteTraceRepository, Trace, TraceQuery, default_trace_db_path
from observability.sqlite_store import DB_SCHEMA_VERSION


def _trace(
    request_id: str,
    status: str = "success",
    created_at: str = None,
    session_id: str = None,
    entrypoint: str = "cli",
) -> Trace:
    trace = Trace(request_id=request_id, created_at=created_at or Trace().created_at)
    with trace.start_span(
        "agent.invoke",
        span_kind="agent.invoke",
        session_id=session_id or f"session:{request_id}",
        entrypoint=entrypoint,
    ):
        with trace.start_span("llm.chat", span_kind="llm.chat", token="secret-token"):
            pass
    trace.finish(status)
    return trace


def test_sqlite_repository_appends_and_queries_records(tmp_path):
    repo = SqliteTraceRepository(tmp_path / "traces.db")
    first = _trace("first")
    second = _trace("second", status="retry")

    repo.append_record(first.to_record())
    repo.append_record(second.to_record())

    assert [record["request_id"] for record in repo.read_records()] == ["first", "second"]
    assert [record["request_id"] for record in repo.list_recent(limit=1)] == ["second"]
    assert [record["request_id"] for record in repo.list_recent(limit=10, status="success")] == ["first"]
    assert repo.get("first")["request_id"] == "first"
    assert repo.get("missing") is None


def test_sqlite_repository_indexes_spans_and_redacts(tmp_path):
    path = tmp_path / "traces.db"
    repo = SqliteTraceRepository(path)
    trace = _trace("sensitive")

    repo.append_record(trace.to_record())

    record = repo.get("sensitive")
    spans = record["spans"]
    assert spans[1]["attributes"]["token"] == REDACTED

    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT span_kind, status FROM spans WHERE trace_id = ? ORDER BY rowid",
            ("sensitive",),
        ).fetchall()
    assert [row["span_kind"] for row in rows] == ["agent.invoke", "llm.chat"]
    assert {row["status"] for row in rows} == {"success"}


def test_sqlite_repository_query_filters_and_cursor(tmp_path):
    repo = SqliteTraceRepository(tmp_path / "traces.db")
    repo.append_record(_trace("one", created_at="2026-07-15T10:00:00", session_id="s1").to_record())
    repo.append_record(_trace("two", created_at="2026-07-15T11:00:00", session_id="s1").to_record())
    repo.append_record(_trace("three", created_at="2026-07-15T12:00:00", session_id="s2").to_record())

    assert [
        record["request_id"]
        for record in repo.query_records(TraceQuery(limit=10, session_id="s1"))
    ] == ["two", "one"]
    assert [
        record["request_id"]
        for record in repo.query_records(TraceQuery(limit=10, start="2026-07-15T10:30:00", end="2026-07-15T12:00:00"))
    ] == ["three", "two"]
    assert [
        record["request_id"]
        for record in repo.query_records(TraceQuery(limit=10, before="three"))
    ] == ["two", "one"]
    assert repo.query_records(TraceQuery(limit=10, before="missing")) == []


def test_sqlite_repository_sets_db_schema_version(tmp_path):
    path = tmp_path / "traces.db"
    SqliteTraceRepository(path)

    with sqlite3.connect(path) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]

    assert version == DB_SCHEMA_VERSION


def test_sqlite_repository_rebuild_from_records(tmp_path):
    repo = SqliteTraceRepository(tmp_path / "traces.db")
    repo.append_record(_trace("old").to_record())
    repo.rebuild_from_records([_trace("new").to_record()])

    assert [record["request_id"] for record in repo.read_records()] == ["new"]
    assert repo.get("old") is None


def test_sqlite_default_path(tmp_path):
    assert default_trace_db_path(tmp_path) == tmp_path / "data" / "observability" / "traces.db"


def test_jsonl_for_project_dual_writes_sqlite_index(tmp_path):
    store = JsonlTraceStore.for_project(tmp_path)
    trace = _trace("dual-write")

    store.append(trace)

    jsonl_path = tmp_path / "data" / "observability" / "traces.jsonl"
    jsonl_record = json.loads(jsonl_path.read_text(encoding="utf-8").splitlines()[0])
    assert jsonl_record["request_id"] == "dual-write"

    repo = SqliteTraceRepository.for_project(tmp_path)
    indexed = repo.get("dual-write")
    assert indexed["request_id"] == "dual-write"
    assert indexed["spans"][1]["attributes"]["token"] == REDACTED
