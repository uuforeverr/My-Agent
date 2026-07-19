import json

from observability.tracing import (
    JsonlTraceStore,
    REDACTED,
    TRACE_SCHEMA_VERSION,
    Trace,
    default_trace_path,
    format_trace_tree,
    redact_data,
    summarize_trace_record,
)


def test_trace_add_total_and_json():
    trace = Trace(request_id="req-1")
    trace.add("step", value=1)

    assert trace.total_ms() >= 0
    record = trace.to_record()
    assert record["schema_version"] == TRACE_SCHEMA_VERSION
    assert record["trace_id"] == "req-1"
    assert record["request_id"] == "req-1"
    assert record["status"] == "running"
    assert record["ended_at"] is None
    assert record["events"][0]["name"] == "step"
    assert record["events"][0]["value"] == 1

    compact = json.loads(trace.to_json())
    pretty = trace.to_json(pretty=True)
    assert compact["request_id"] == "req-1"
    assert "\n" in pretty


def test_jsonl_trace_store_appends(tmp_path):
    path = tmp_path / "logs" / "traces.jsonl"
    store = JsonlTraceStore(path)
    trace = Trace(request_id="req-2")
    trace.add("done")

    store.append(trace)

    assert path.exists()
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["request_id"] == "req-2"
    assert record["status"] == "success"
    assert record["ended_at"]


def test_trace_records_error():
    trace = Trace(request_id="req-3")
    error = RuntimeError("boom")

    trace.record_error(error, event_name="failed", phase="unit")

    record = trace.to_record()
    assert record["status"] == "error"
    assert record["error"] == {"type": "RuntimeError", "message": "boom"}
    assert record["events"][0]["name"] == "failed"
    assert record["events"][0]["phase"] == "unit"


def test_default_trace_path_points_to_observability_dir(tmp_path):
    assert default_trace_path(tmp_path) == tmp_path / "data" / "observability" / "traces.jsonl"


def test_trace_start_span_builds_parent_child_tree():
    trace = Trace(request_id="req-span")

    with trace.start_span("agent.invoke", span_kind="agent.invoke") as root:
        root.set_attribute("session_id", "cli:local")
        with trace.start_span("llm.chat", span_kind="llm.chat") as child:
            child.add_event("response_received", chars=12)

    record = trace.to_record()
    assert record["root_span_id"] == root.span_id
    assert len(record["spans"]) == 2
    assert record["spans"][0]["status"] == "success"
    assert record["spans"][0]["start_offset_ms"] >= 0
    assert record["spans"][1]["start_offset_ms"] >= record["spans"][0]["start_offset_ms"]
    assert record["spans"][1]["parent_span_id"] == root.span_id
    assert record["spans"][1]["events"][0]["name"] == "response_received"


def test_trace_start_span_records_exceptions():
    trace = Trace(request_id="req-error")

    try:
        with trace.start_span("tool.execute", span_kind="tool.execute"):
            raise RuntimeError("tool exploded")
    except RuntimeError:
        pass

    record = trace.to_record()
    assert record["status"] == "error"
    assert record["error"]["type"] == "RuntimeError"
    assert record["spans"][0]["status"] == "error"
    assert record["spans"][0]["error"]["message"] == "tool exploded"


def test_jsonl_trace_store_reads_lists_and_gets_records(tmp_path):
    path = tmp_path / "traces.jsonl"
    store = JsonlTraceStore(path)
    first = Trace(request_id="first")
    with first.start_span("agent.invoke", span_kind="agent.invoke", session_id="s1"):
        pass
    second = Trace(request_id="second")
    with second.start_span("scheduler.execute", span_kind="scheduler.execute"):
        pass
    second.finish("retry")

    store.append(first)
    store.append(second)

    assert [record["request_id"] for record in store.read_records()] == ["first", "second"]
    assert [record["request_id"] for record in store.list_recent(limit=1)] == ["second"]
    assert [record["request_id"] for record in store.list_recent(limit=10, status="success")] == ["first"]
    assert store.get("first")["request_id"] == "first"
    assert store.get("missing") is None


def test_jsonl_trace_store_skips_bad_json_lines(tmp_path):
    path = tmp_path / "traces.jsonl"
    path.write_text('{"trace_id":"ok"}\n{bad-json\n', encoding="utf-8")
    store = JsonlTraceStore(path)

    assert store.read_records() == [{"trace_id": "ok"}]


def test_jsonl_trace_store_redacts_records_on_read(tmp_path):
    path = tmp_path / "traces.jsonl"
    path.write_text(
        '{"trace_id":"legacy","events":[{"api_key":"sk-legacysecret"}]}\n',
        encoding="utf-8",
    )
    store = JsonlTraceStore(path)

    assert store.read_records()[0]["events"][0]["api_key"] == REDACTED


def test_trace_summary_and_tree_formatting():
    trace = Trace(request_id="tree")
    with trace.start_span("agent.invoke", span_kind="agent.invoke", session_id="cli:local"):
        with trace.start_span("llm.chat", span_kind="llm.chat") as child:
            child.set_attribute("model", "fake")

    record = trace.to_record()
    summary = summarize_trace_record(record)
    assert summary["trace_id"] == "tree"
    assert summary["root_name"] == "agent.invoke"
    assert summary["session_id"] == "cli:local"
    tree = format_trace_tree(record)
    assert "- agent.invoke [success]" in tree
    assert "  - llm.chat [success]" in tree


def test_trace_record_redacts_sensitive_fields():
    trace = Trace(request_id="redact")
    trace.add(
        "request",
        api_key="sk-supersecret",
        headers={"Authorization": "Bearer abc123"},
        message="call with bce-v3/private-token",
    )
    with trace.start_span(
        "tool.execute",
        span_kind="tool.execute",
        token="plain-token",
        safe_value="visible",
    ) as span:
        span.add_event("sent", cookie="session=abc", text="Bearer nested-token")

    record = trace.to_record()
    event = record["events"][0]
    assert event["api_key"] == REDACTED
    assert event["headers"]["Authorization"] == REDACTED
    assert REDACTED in event["message"]
    span_record = record["spans"][0]
    assert span_record["attributes"]["token"] == REDACTED
    assert span_record["attributes"]["safe_value"] == "visible"
    assert span_record["events"][0]["cookie"] == REDACTED
    assert span_record["events"][0]["text"] == REDACTED


def test_redact_data_handles_nested_values():
    data = {
        "openai_api_key": "sk-abc123456",
        "nested": [{"secret": "value"}, {"text": "hello"}],
    }

    assert redact_data(data) == {
        "openai_api_key": REDACTED,
        "nested": [{"secret": REDACTED}, {"text": "hello"}],
    }
