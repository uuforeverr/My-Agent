import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from observability.api import RepairingTraceRepository, TraceQueryService, create_trace_api_server, make_trace_api_handler
from observability.sqlite_store import SqliteTraceRepository, default_trace_db_path
from observability.tracing import JsonlTraceStore, Trace, default_trace_path


def _seed_store(path):
    store = JsonlTraceStore(path)
    success = Trace(request_id="trace-success")
    with success.start_span("agent.invoke", span_kind="agent.invoke", session_id="cli:local"):
        with success.start_span("llm.chat", span_kind="llm.chat"):
            pass
    error = Trace(request_id="trace-error")
    try:
        with error.start_span("tool.execute", span_kind="tool.execute"):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    store.append(success)
    store.append(error)
    return store


def _get_json(base_url: str, path: str, headers=None):
    req = urllib.request.Request(f"{base_url}{path}", headers=headers or {})
    with urllib.request.urlopen(req, timeout=2) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def _get_text(base_url: str, path: str, headers=None):
    req = urllib.request.Request(f"{base_url}{path}", headers=headers or {})
    with urllib.request.urlopen(req, timeout=2) as resp:
        return resp.status, resp.read().decode("utf-8")


def _post_json(base_url: str, path: str, headers=None):
    req = urllib.request.Request(f"{base_url}{path}", data=b"", headers=headers or {}, method="POST")
    with urllib.request.urlopen(req, timeout=2) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def test_trace_query_service_overview_and_detail(tmp_path):
    store = _seed_store(tmp_path / "traces.jsonl")
    service = TraceQueryService(store)

    overview = service.overview()
    assert overview["trace_count"] == 2
    assert overview["status_counts"] == {"success": 1, "error": 1}
    assert overview["span_kind_counts"]["agent.invoke"] == 1
    assert overview["span_kind_counts"]["tool.execute"] == 1

    traces = service.list_traces(limit=1)
    assert traces["traces"][0]["trace_id"] == "trace-error"
    assert traces["next_cursor"] == "trace-error"

    detail = service.get_trace("trace-success")
    assert detail["summary"]["trace_id"] == "trace-success"
    assert "agent.invoke" in detail["span_tree"]
    assert service.get_trace("missing") is None


def test_trace_api_http_endpoints(tmp_path):
    store = _seed_store(tmp_path / "traces.jsonl")
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_trace_api_handler(store))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    base_url = f"http://{host}:{port}"

    try:
        status, html = _get_text(base_url, "/dashboard")
        assert status == 200
        assert "my-agent trace explorer" in html
        assert "/api/traces" in html
        assert "/api/auth/check" in html
        assert "/bundle" in html
        assert "Download bundle" in html
        assert "noticeBox" in html
        assert 'id="loadMoreBtn" class="secondary" disabled' in html

        status, payload = _get_json(base_url, "/api/health")
        assert status == 200
        assert payload == {"ok": True}

        status, payload = _get_json(base_url, "/api/auth/check")
        assert status == 200
        assert payload == {"ok": True, "authenticated": True, "auth_required": False}

        status, payload = _get_json(base_url, "/api/overview")
        assert status == 200
        assert payload["trace_count"] == 2

        status, payload = _get_json(base_url, "/api/traces?limit=1")
        assert status == 200
        assert payload["traces"][0]["trace_id"] == "trace-error"
        assert payload["next_cursor"] == "trace-error"

        status, payload = _get_json(base_url, "/api/traces?limit=1&before=trace-error")
        assert status == 200
        assert payload["traces"][0]["trace_id"] == "trace-success"

        status, payload = _get_json(base_url, "/api/traces?status=success")
        assert status == 200
        assert [item["trace_id"] for item in payload["traces"]] == ["trace-success"]

        status, payload = _get_json(base_url, "/api/traces?session_id=cli:local")
        assert status == 200
        assert [item["trace_id"] for item in payload["traces"]] == ["trace-success"]

        status, payload = _get_json(base_url, "/api/traces/trace-success")
        assert status == 200
        assert payload["summary"]["trace_id"] == "trace-success"
        assert "llm.chat" in payload["span_tree"]

        status, payload = _get_json(base_url, "/api/traces/trace-success/bundle")
        assert status == 200
        assert payload["bundle_version"] == "1.0"
        assert payload["summary"]["trace_id"] == "trace-success"
        assert payload["trace"]["trace_id"] == "trace-success"
        assert "span_tree" in payload
        assert "error_spans" in payload

        try:
            _get_json(base_url, "/api/traces/missing")
            assert False, "expected 404"
        except urllib.error.HTTPError as e:
            assert e.code == 404
            body = json.loads(e.read().decode("utf-8"))
            assert body["error"] == "trace_not_found"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_trace_api_requires_token_when_configured(tmp_path):
    store = _seed_store(tmp_path / "traces.jsonl")
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        make_trace_api_handler(store, api_token="dev-token"),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    base_url = f"http://{host}:{port}"

    try:
        status, html = _get_text(base_url, "/dashboard")
        assert status == 200
        assert "trace_api_token" in html

        try:
            _get_json(base_url, "/api/overview")
            assert False, "expected 401"
        except urllib.error.HTTPError as e:
            assert e.code == 401
            body = json.loads(e.read().decode("utf-8"))
            assert body["error"] == "unauthorized"

        status, payload = _get_json(
            base_url,
            "/api/auth/check",
            headers={"Authorization": "Bearer dev-token"},
        )
        assert status == 200
        assert payload == {"ok": True, "authenticated": True, "auth_required": True}

        status, payload = _get_json(
            base_url,
            "/api/overview",
            headers={"Authorization": "Bearer dev-token"},
        )
        assert status == 200
        assert payload["trace_count"] == 2

        status, payload = _get_json(
            base_url,
            "/api/traces?limit=1",
            headers={"X-Trace-Api-Token": "dev-token"},
        )
        assert status == 200
        assert payload["traces"][0]["trace_id"] == "trace-error"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_repairing_repository_falls_back_to_jsonl_and_repairs_sqlite(tmp_path):
    jsonl = _seed_store(tmp_path / "traces.jsonl")
    sqlite = SqliteTraceRepository(tmp_path / "traces.db")
    repository = RepairingTraceRepository(sqlite, jsonl)

    detail = repository.get("trace-success")

    assert detail["trace_id"] == "trace-success"
    assert sqlite.get("trace-success")["trace_id"] == "trace-success"
    result = repository.repair_index()
    assert result["total"] == 2
    assert result["repaired"] == 1
    assert sqlite.get("trace-error")["trace_id"] == "trace-error"


def test_trace_api_repair_and_rebuild_endpoints(tmp_path):
    jsonl = _seed_store(tmp_path / "traces.jsonl")
    sqlite = SqliteTraceRepository(tmp_path / "traces.db")
    repository = RepairingTraceRepository(sqlite, jsonl)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_trace_api_handler(repository))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    base_url = f"http://{host}:{port}"

    try:
        status, payload = _post_json(base_url, "/api/index/repair")
        assert status == 200
        assert payload["mode"] == "repair"
        assert payload["total"] == 2
        assert payload["repaired"] == 2

        status, payload = _post_json(base_url, "/api/index/rebuild")
        assert status == 200
        assert payload["mode"] == "rebuild"
        assert payload["total"] == 2
        assert payload["repaired"] == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_create_trace_api_server_indexes_existing_jsonl(tmp_path):
    jsonl_store = JsonlTraceStore(default_trace_path(tmp_path))
    trace = Trace(request_id="from-jsonl")
    with trace.start_span("agent.invoke", span_kind="agent.invoke"):
        pass
    jsonl_store.append(trace)

    server = create_trace_api_server(project_root=tmp_path, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    base_url = f"http://{host}:{port}"

    try:
        status, payload = _get_json(base_url, "/api/traces?limit=5")
        assert status == 200
        assert payload["traces"][0]["trace_id"] == "from-jsonl"
        assert default_trace_db_path(tmp_path).exists()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
