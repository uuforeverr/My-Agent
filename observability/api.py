import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from datetime import datetime
from typing import Any, Optional, Protocol
from urllib.parse import parse_qs, unquote, urlparse

from observability.dashboard import dashboard_html
from observability.tracing import (
    JsonlTraceStore,
    TraceQuery,
    default_trace_path,
    format_trace_tree,
    summarize_trace_record,
)
from observability.sqlite_store import SqliteTraceRepository


class TraceRepository(Protocol):
    def read_records(self) -> list[dict[str, Any]]:
        ...

    def list_recent(self, limit: int = 10, status: Optional[str] = None) -> list[dict[str, Any]]:
        ...

    def query_records(self, query: TraceQuery) -> list[dict[str, Any]]:
        ...

    def get(self, trace_id: str) -> Optional[dict[str, Any]]:
        ...


class RepairingTraceRepository:
    def __init__(self, primary: SqliteTraceRepository, fallback: JsonlTraceStore):
        self.primary = primary
        self.fallback = fallback

    def read_records(self) -> list[dict[str, Any]]:
        return self.primary.read_records()

    def list_recent(self, limit: int = 10, status: Optional[str] = None) -> list[dict[str, Any]]:
        return self.query_records(TraceQuery(limit=limit, status=status))

    def query_records(self, query: TraceQuery) -> list[dict[str, Any]]:
        records = self.primary.query_records(query)
        if records:
            return records
        fallback_records = self.fallback.query_records(query)
        for record in fallback_records:
            self.primary.append_record(record)
        return fallback_records

    def get(self, trace_id: str) -> Optional[dict[str, Any]]:
        record = self.primary.get(trace_id)
        if record is not None:
            return record
        record = self.fallback.get(trace_id)
        if record is not None:
            self.primary.append_record(record)
        return record

    def repair_index(self) -> dict[str, int]:
        fallback_records = self.fallback.read_records()
        existing = self.primary.trace_ids()
        repaired = 0
        for record in fallback_records:
            trace_id = str(record.get("trace_id") or record.get("request_id") or "")
            if trace_id and trace_id not in existing:
                self.primary.append_record(record)
                existing.add(trace_id)
                repaired += 1
        return {"total": len(fallback_records), "repaired": repaired}

    def rebuild_index(self) -> dict[str, int]:
        fallback_records = self.fallback.read_records()
        self.primary.rebuild_from_records(fallback_records)
        return {"total": len(fallback_records), "repaired": len(fallback_records)}


class TraceQueryService:
    def __init__(self, repository: TraceRepository):
        self.repository = repository

    def overview(self) -> dict[str, Any]:
        records = self.repository.read_records()
        status_counts: dict[str, int] = {}
        span_kind_counts: dict[str, int] = {}
        total_ms_values: list[float] = []

        for record in records:
            status = str(record.get("status") or "unknown")
            status_counts[status] = status_counts.get(status, 0) + 1
            total_ms = record.get("total_ms")
            if isinstance(total_ms, (int, float)):
                total_ms_values.append(float(total_ms))
            for span in record.get("spans") or []:
                span_kind = str(span.get("span_kind") or "unknown")
                span_kind_counts[span_kind] = span_kind_counts.get(span_kind, 0) + 1

        return {
            "trace_count": len(records),
            "status_counts": status_counts,
            "span_kind_counts": span_kind_counts,
            "avg_total_ms": _average(total_ms_values),
            "latest_created_at": records[-1].get("created_at") if records else None,
        }

    def list_traces(
        self,
        limit: int = 10,
        status: Optional[str] = None,
        before: Optional[str] = None,
        session_id: Optional[str] = None,
        entrypoint: Optional[str] = None,
        start: Optional[str] = None,
        end: Optional[str] = None,
    ) -> dict[str, Any]:
        query = TraceQuery(
            limit=limit,
            before=before,
            status=status,
            session_id=session_id,
            entrypoint=entrypoint,
            start=start,
            end=end,
        )
        fetch_query = query.with_limit(query.safe_limit + 1)
        records = self.repository.query_records(fetch_query)
        has_more = len(records) > query.safe_limit
        records = records[:query.safe_limit]
        return {
            "traces": [summarize_trace_record(record) for record in records],
            "limit": query.safe_limit,
            "status": status,
            "before": before,
            "session_id": session_id,
            "entrypoint": entrypoint,
            "start": start,
            "end": end,
            "next_cursor": (
                (records[-1].get("trace_id") or records[-1].get("request_id"))
                if has_more and records
                else None
            ),
        }

    def get_trace(self, trace_id: str) -> Optional[dict[str, Any]]:
        record = self.repository.get(trace_id)
        if record is None:
            return None
        return {
            "summary": summarize_trace_record(record),
            "span_tree": format_trace_tree(record),
            "trace": record,
        }

    def get_debug_bundle(self, trace_id: str) -> Optional[dict[str, Any]]:
        detail = self.get_trace(trace_id)
        if detail is None:
            return None
        trace = detail["trace"]
        spans = trace.get("spans") or []
        error_spans = [
            span
            for span in spans
            if span.get("status") == "error" or span.get("error")
        ]
        return {
            "bundle_version": "1.0",
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "summary": detail["summary"],
            "span_tree": detail["span_tree"],
            "error_spans": error_spans,
            "trace": trace,
        }

    def repair_index(self, rebuild: bool = False) -> dict[str, Any]:
        if rebuild and hasattr(self.repository, "rebuild_index"):
            result = self.repository.rebuild_index()
            return {"ok": True, "mode": "rebuild", **result}
        if hasattr(self.repository, "repair_index"):
            result = self.repository.repair_index()
            return {"ok": True, "mode": "repair", **result}
        return {"ok": False, "reason": "repository_does_not_support_repair"}


def make_trace_api_handler(repository: TraceRepository, api_token: str = ""):
    service = TraceQueryService(repository)
    required_token = (api_token or "").strip()

    class TraceApiHandler(BaseHTTPRequestHandler):
        server_version = "MyAgentTraceAPI/0.1"

        def do_OPTIONS(self) -> None:
            self._send_json({"ok": True})

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            query = parse_qs(parsed.query)

            if path in {"/", "/dashboard"}:
                self._send_html(dashboard_html())
                return

            if not self._is_authorized():
                self._send_json({"error": "unauthorized"}, status=401)
                return

            if path == "/api/health":
                self._send_json({"ok": True})
                return

            if path == "/api/overview":
                self._send_json(service.overview())
                return

            if path == "/api/auth/check":
                self._send_json({"ok": True, "authenticated": True, "auth_required": bool(required_token)})
                return

            if path == "/api/traces":
                limit = _parse_int(_first(query.get("limit")), default=10)
                status = _first(query.get("status"))
                self._send_json(
                    service.list_traces(
                        limit=limit,
                        status=status,
                        before=_first(query.get("before")),
                        session_id=_first(query.get("session_id")),
                        entrypoint=_first(query.get("entrypoint")),
                        start=_first(query.get("start")),
                        end=_first(query.get("end")),
                    )
                )
                return

            prefix = "/api/traces/"
            if path.startswith(prefix):
                suffix = path[len(prefix):].strip()
                if suffix.endswith("/bundle"):
                    trace_id = unquote(suffix[:-len("/bundle")]).strip()
                    payload = service.get_debug_bundle(trace_id)
                    if payload is None:
                        self._send_json({"error": "trace_not_found", "trace_id": trace_id}, status=404)
                        return
                    self._send_json(payload)
                    return

                trace_id = unquote(suffix).strip()
                payload = service.get_trace(trace_id)
                if payload is None:
                    self._send_json({"error": "trace_not_found", "trace_id": trace_id}, status=404)
                    return
                self._send_json(payload)
                return

            self._send_json({"error": "not_found", "path": path}, status=404)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            query = parse_qs(parsed.query)

            if not self._is_authorized():
                self._send_json({"error": "unauthorized"}, status=401)
                return

            if path == "/api/index/rebuild":
                rebuild = _parse_bool(_first(query.get("rebuild")), default=True)
                self._send_json(service.repair_index(rebuild=rebuild))
                return

            if path == "/api/index/repair":
                self._send_json(service.repair_index(rebuild=False))
                return

            self._send_json({"error": "not_found", "path": path}, status=404)

        def log_message(self, format: str, *args: Any) -> None:  # pragma: no cover - keep CLI quiet
            return

        def _is_authorized(self) -> bool:
            if not required_token:
                return True
            header_token = (self.headers.get("X-Trace-Api-Token") or "").strip()
            if header_token == required_token:
                return True
            auth = (self.headers.get("Authorization") or "").strip()
            prefix = "Bearer "
            return auth.startswith(prefix) and auth[len(prefix):].strip() == required_token

        def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, html: str, status: int = 200) -> None:
            body = html.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return TraceApiHandler


def create_trace_api_server(
    project_root: Path,
    host: str = "127.0.0.1",
    port: int = 8765,
    api_token: str = "",
) -> ThreadingHTTPServer:
    repository = _default_repository(project_root)
    handler = make_trace_api_handler(repository, api_token=api_token)
    return ThreadingHTTPServer((host, port), handler)


def _default_repository(project_root: Path) -> TraceRepository:
    jsonl_store = JsonlTraceStore(default_trace_path(project_root))
    try:
        sqlite_repo = SqliteTraceRepository.for_project(project_root)
        repository = RepairingTraceRepository(sqlite_repo, jsonl_store)
        repository.repair_index()
        return repository
    except Exception:
        return jsonl_store


def main() -> None:  # pragma: no cover - process entrypoint
    parser = argparse.ArgumentParser(description="Run the my-agent read-only trace API.")
    parser.add_argument("--project-root", default=str(Path(__file__).resolve().parent.parent))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--token", default=os.environ.get("TRACE_API_TOKEN", ""))
    args = parser.parse_args()

    server = create_trace_api_server(
        project_root=Path(args.project_root).expanduser().resolve(),
        host=args.host,
        port=args.port,
        api_token=args.token,
    )
    host, port = server.server_address
    print(f"trace api: http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        server.server_close()


def _average(values: list[float]) -> Optional[float]:
    if not values:
        return None
    return round(sum(values) / len(values), 2)


def _first(values: Optional[list[str]]) -> Optional[str]:
    if not values:
        return None
    value = values[0].strip()
    return value or None


def _parse_int(raw: Optional[str], default: int) -> int:
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _parse_bool(raw: Optional[str], default: bool = False) -> bool:
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


if __name__ == "__main__":  # pragma: no cover
    main()
