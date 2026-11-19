from types import SimpleNamespace

from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import observability.otel as otel


def _settings(tmp_path):
    return SimpleNamespace(
        project_root=tmp_path,
        otel_enabled=False,
        otel_capture_content=True,
        otel_content_max_bytes=100000,
        otel_service_name="my-agent-test",
        otel_environment="test",
    )


def test_native_otel_parent_child_and_content_redaction(tmp_path):
    otel.shutdown_observability()
    exporter = InMemorySpanExporter()
    provider = otel.configure_observability(_settings(tmp_path))
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    with otel.start_span("turn my-agent", root=True) as root:
        otel.add_content_event(root, "my_agent.user_message", {"text": "hello", "api_key": "sk-secretsecret"})
        with otel.start_span("invoke_agent my-agent"):
            with otel.start_span("chat fake-model"):
                pass
    provider.force_flush()

    spans = {span.name: span for span in exporter.get_finished_spans()}
    assert spans["invoke_agent my-agent"].parent.span_id == spans["turn my-agent"].context.span_id
    assert spans["chat fake-model"].parent.span_id == spans["invoke_agent my-agent"].context.span_id
    event = spans["turn my-agent"].events[0]
    assert "sk-secretsecret" not in event.attributes["content"]
    assert "[REDACTED]" in event.attributes["content"]
    otel.shutdown_observability()


def test_redact_data_handles_headers_and_nested_secrets():
    redacted = otel.redact_data(
        {
            "Authorization": "Bearer abc.def",
            "nested": {"password": "p", "message": "key sk-abcdefghijklmnop"},
        }
    )
    assert redacted["Authorization"] == "[REDACTED]"
    assert redacted["nested"]["password"] == "[REDACTED]"
    assert "sk-abcdefghijklmnop" not in redacted["nested"]["message"]


class _FailingExporter(SpanExporter):
    def export(self, spans):
        return SpanExportResult.FAILURE

    def shutdown(self):
        return None


def test_export_failure_is_fail_open_for_business_code(tmp_path):
    otel.shutdown_observability()
    settings = _settings(tmp_path)
    settings.otel_enabled = True
    provider = otel.configure_observability(settings, exporter=_FailingExporter())
    completed = []

    with otel.start_span("turn my-agent", root=True):
        completed.append("business result")

    assert completed == ["business result"]
    provider.force_flush(timeout_millis=1000)
    otel.shutdown_observability()
