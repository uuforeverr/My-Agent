import json
import re
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from threading import Lock, RLock
from typing import Any, Generator, Optional

TRACE_SCHEMA_VERSION = "1.2"
REDACTED = "[REDACTED]"
SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "password",
    "secret",
    "tenant_access_token",
)
SENSITIVE_VALUE_PATTERNS = (
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bbce-v3/[A-Za-z0-9._~+/=-]+\b"),
)


def default_trace_path(project_root: Path) -> Path:
    return project_root / "data" / "observability" / "traces.jsonl"


@dataclass(frozen=True)
class TraceQuery:
    limit: int = 10
    before: Optional[str] = None
    status: Optional[str] = None
    session_id: Optional[str] = None
    entrypoint: Optional[str] = None
    start: Optional[str] = None
    end: Optional[str] = None

    @property
    def safe_limit(self) -> int:
        return max(1, min(int(self.limit), 100))

    def with_limit(self, limit: int) -> "TraceQuery":
        return TraceQuery(
            limit=limit,
            before=self.before,
            status=self.status,
            session_id=self.session_id,
            entrypoint=self.entrypoint,
            start=self.start,
            end=self.end,
        )


def redact_data(value: Any, key: str = "") -> Any:
    if _is_sensitive_key(key):
        return REDACTED
    if isinstance(value, dict):
        return {str(k): redact_data(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_data(item, key) for item in value]
    if isinstance(value, tuple):
        return [redact_data(item, key) for item in value]
    if isinstance(value, str):
        redacted = value
        for pattern in SENSITIVE_VALUE_PATTERNS:
            redacted = pattern.sub(REDACTED, redacted)
        return redacted
    return value


def _is_sensitive_key(key: str) -> bool:
    normalized = (key or "").lower().replace("-", "_")
    return (
        any(part in normalized for part in SENSITIVE_KEY_PARTS)
        or normalized == "token"
        or normalized.endswith("_token")
    )


@dataclass
class Span:
    trace_id: str
    name: str
    span_kind: str = "internal"
    parent_span_id: Optional[str] = None
    span_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    started_at: float = field(default_factory=time.monotonic)
    start_offset_ms: Optional[float] = None
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    ended_at: Optional[str] = None
    duration_ms: Optional[float] = None
    status: str = "running"
    attributes: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    error: Optional[dict[str, Any]] = None

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def set_attributes(self, **attributes: Any) -> None:
        self.attributes.update(attributes)

    def add_event(self, name: str, **attributes: Any) -> None:
        self.events.append({
            "name": name,
            "elapsed_ms": self.elapsed_ms(),
            **attributes,
        })

    def set_status(self, status: str) -> None:
        self.status = status

    def record_error(self, error: Exception) -> None:
        self.status = "error"
        self.error = {
            "type": type(error).__name__,
            "message": str(error),
        }
        self.add_event(
            "exception",
            error_type=self.error["type"],
            error_message=self.error["message"],
        )

    def elapsed_ms(self) -> float:
        return round((time.monotonic() - self.started_at) * 1000, 2)

    def finish(self, status: str = "success") -> None:
        if self.status == "running":
            self.status = status
        self.ended_at = self.ended_at or datetime.now().isoformat(timespec="seconds")
        self.duration_ms = self.duration_ms if self.duration_ms is not None else self.elapsed_ms()

    def to_record(self) -> dict[str, Any]:
        return {
            "span_id": self.span_id,
            "trace_id": self.trace_id,
            "parent_span_id": self.parent_span_id,
            "name": self.name,
            "span_kind": self.span_kind,
            "start_offset_ms": self.start_offset_ms,
            "created_at": self.created_at,
            "ended_at": self.ended_at,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "attributes": redact_data(self.attributes),
            "events": redact_data(self.events),
            "error": redact_data(self.error),
        }


@dataclass
class Trace:
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    trace_id: Optional[str] = None
    started_at: float = field(default_factory=time.monotonic)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    ended_at: Optional[str] = None
    status: str = "running"
    error: Optional[dict[str, Any]] = None
    events: list[dict] = field(default_factory=list)
    spans: list[Span] = field(default_factory=list)
    _span_stack: list[str] = field(default_factory=list, init=False, repr=False)
    _lock: RLock = field(default_factory=RLock, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.trace_id is None:
            self.trace_id = self.request_id

    def add(self, name: str, **data) -> None:
        with self._lock:
            self.events.append({
                "name": name,
                "elapsed_ms": round((time.monotonic() - self.started_at) * 1000, 2),
                **data,
            })

    @contextmanager
    def start_span(
        self,
        name: str,
        span_kind: str = "internal",
        parent_span_id: Optional[str] = None,
        **attributes: Any,
    ) -> Generator[Span, None, None]:
        with self._lock:
            parent_id = parent_span_id
            if parent_id is None and self._span_stack:
                parent_id = self._span_stack[-1]
            span = Span(
                trace_id=str(self.trace_id),
                name=name,
                span_kind=span_kind,
                parent_span_id=parent_id,
                attributes=dict(attributes),
            )
            span.start_offset_ms = round((span.started_at - self.started_at) * 1000, 2)
            self.spans.append(span)
            self._span_stack.append(span.span_id)

        try:
            yield span
        except Exception as e:
            span.record_error(e)
            self._mark_error(e)
            raise
        finally:
            span.finish()
            with self._lock:
                if self._span_stack and self._span_stack[-1] == span.span_id:
                    self._span_stack.pop()
                elif span.span_id in self._span_stack:
                    self._span_stack.remove(span.span_id)

    def finish(self, status: str = "success") -> None:
        if self.status == "running":
            self.status = status
        self.ended_at = self.ended_at or datetime.now().isoformat(timespec="seconds")

    def _mark_error(self, error: Exception) -> dict[str, Any]:
        error_record = {
            "type": type(error).__name__,
            "message": str(error),
        }
        self.status = "error"
        self.error = error_record
        return error_record

    def record_error(self, error: Exception, event_name: str = "error", **data) -> None:
        error_record = self._mark_error(error)
        self.add(
            event_name,
            status="error",
            error_type=error_record["type"],
            error_message=error_record["message"],
            **data,
        )
        self.ended_at = self.ended_at or datetime.now().isoformat(timespec="seconds")

    def total_ms(self) -> float:
        return round((time.monotonic() - self.started_at) * 1000, 2)

    def to_record(self) -> dict:
        root_span_id = next((span.span_id for span in self.spans if span.parent_span_id is None), None)
        return {
            "schema_version": TRACE_SCHEMA_VERSION,
            "trace_id": self.trace_id,
            "request_id": self.request_id,
            "created_at": self.created_at,
            "ended_at": self.ended_at,
            "status": self.status,
            "error": redact_data(self.error),
            "total_ms": self.total_ms(),
            "root_span_id": root_span_id,
            "events": redact_data(self.events),
            "spans": [span.to_record() for span in self.spans],
        }

    def to_json(self, *, pretty: bool = False) -> str:
        return json.dumps(
            self.to_record(),
            ensure_ascii=False,
            indent=2 if pretty else None,
            default=str,
        )


class JsonlTraceStore:
    def __init__(self, path: Path, index_repository: Any = None):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.index_repository = index_repository
        self._lock = Lock()

    @classmethod
    def for_project(cls, project_root: Path) -> "JsonlTraceStore":
        try:
            from observability.sqlite_store import SqliteTraceRepository

            index_repository = SqliteTraceRepository.for_project(project_root)
        except Exception:
            index_repository = None
        return cls(default_trace_path(project_root), index_repository=index_repository)

    def append(self, trace: Trace) -> None:
        trace.finish()
        record = trace.to_record()
        with self._lock:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            if self.index_repository is not None:
                try:
                    self.index_repository.append_record(record)
                except Exception:
                    pass

    def read_records(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with self._lock:
            lines = self.path.read_text(encoding="utf-8").splitlines()

        records: list[dict[str, Any]] = []
        for line in lines:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                records.append(redact_data(record))
        return records

    def list_recent(
        self,
        limit: int = 10,
        status: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        return self.query_records(TraceQuery(limit=limit, status=status))

    def query_records(self, query: TraceQuery) -> list[dict[str, Any]]:
        records = reversed(self.read_records())
        selected: list[dict[str, Any]] = []
        if query.before:
            records = _skip_until_cursor(records, query.before)
        for record in records:
            if not _matches_trace_query(record, query):
                continue
            selected.append(record)
            if len(selected) >= query.safe_limit:
                break
        return selected

    def get(self, trace_id: str) -> Optional[dict[str, Any]]:
        wanted = (trace_id or "").strip()
        if not wanted:
            return None
        for record in reversed(self.read_records()):
            if record.get("trace_id") == wanted or record.get("request_id") == wanted:
                return record
        return None


def summarize_trace_record(record: dict[str, Any]) -> dict[str, Any]:
    spans = record.get("spans") or []
    root_span = next(
        (span for span in spans if span.get("span_id") == record.get("root_span_id")),
        spans[0] if spans else {},
    )
    attributes = root_span.get("attributes") or {}
    return {
        "trace_id": record.get("trace_id") or record.get("request_id") or "",
        "created_at": record.get("created_at") or "",
        "status": record.get("status") or "",
        "total_ms": record.get("total_ms"),
        "span_count": len(spans),
        "root_name": root_span.get("name") or "",
        "session_id": attributes.get("session_id") or "",
        "entrypoint": attributes.get("entrypoint") or attributes.get("channel_type") or "",
    }


def format_trace_tree(record: dict[str, Any]) -> str:
    spans = record.get("spans") or []
    if not spans:
        return "(no spans)"

    children: dict[Optional[str], list[dict[str, Any]]] = {}
    for span in spans:
        children.setdefault(span.get("parent_span_id"), []).append(span)

    lines: list[str] = []

    def _walk(span: dict[str, Any], depth: int) -> None:
        indent = "  " * depth
        duration = span.get("duration_ms")
        duration_text = f"{duration}ms" if duration is not None else "-"
        status = span.get("status") or "-"
        kind = span.get("span_kind") or "-"
        lines.append(f"{indent}- {span.get('name') or kind} [{status}] {duration_text}")
        for child in children.get(span.get("span_id"), []):
            _walk(child, depth + 1)

    roots = children.get(None) or [spans[0]]
    for root in roots:
        _walk(root, 0)
    return "\n".join(lines)


def _skip_until_cursor(records, cursor: str):
    found = False
    for record in records:
        trace_id = record.get("trace_id") or record.get("request_id")
        if not found:
            if trace_id == cursor:
                found = True
            continue
        yield record


def _matches_trace_query(record: dict[str, Any], query: TraceQuery) -> bool:
    if query.status and record.get("status") != query.status:
        return False
    summary = summarize_trace_record(record)
    if query.session_id and summary.get("session_id") != query.session_id:
        return False
    if query.entrypoint and summary.get("entrypoint") != query.entrypoint:
        return False
    created_at = record.get("created_at") or ""
    if query.start and created_at < query.start:
        return False
    if query.end and created_at > query.end:
        return False
    return True
