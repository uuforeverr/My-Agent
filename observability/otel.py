from __future__ import annotations

import hashlib
import json
import logging
import re
import socket
import subprocess
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_ON, ParentBased
from opentelemetry.trace import Span, Status, StatusCode

LOGGER = logging.getLogger(__name__)
REDACTED = "[REDACTED]"
_provider: Optional[TracerProvider] = None
_provider_lock = threading.Lock()
_capture_content = True
_max_content_bytes = 1_000_000
_sensitive_key = re.compile(
    r"(?:api[_-]?key|authorization|cookie|password|passwd|secret|access[_-]?token|refresh[_-]?token)",
    re.IGNORECASE,
)
_bearer = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+\-/=]+")
_openai_key = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b")


def configure_observability(settings: Any, exporter: Optional[SpanExporter] = None) -> TracerProvider:
    """Configure one process-level provider. Export failures remain off the request path."""
    global _provider, _capture_content, _max_content_bytes
    with _provider_lock:
        if _provider is not None:
            return _provider

        _capture_content = bool(getattr(settings, "otel_capture_content", True))
        _max_content_bytes = max(4096, int(getattr(settings, "otel_content_max_bytes", 1_000_000)))
        resource = Resource.create(
            {
                "service.name": getattr(settings, "otel_service_name", "my-agent"),
                "service.version": _service_version(getattr(settings, "project_root", Path.cwd())),
                "service.instance.id": socket.gethostname(),
                "deployment.environment.name": getattr(settings, "otel_environment", "local"),
            }
        )
        provider = TracerProvider(resource=resource, sampler=ParentBased(ALWAYS_ON))
        if bool(getattr(settings, "otel_enabled", False)):
            if exporter is None:
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

                endpoint = str(getattr(settings, "otel_endpoint", "http://127.0.0.1:4318")).rstrip("/")
                if not endpoint.endswith("/v1/traces"):
                    endpoint += "/v1/traces"
                exporter = OTLPSpanExporter(endpoint=endpoint, timeout=2)
            provider.add_span_processor(
                BatchSpanProcessor(
                    exporter,
                    max_queue_size=2048,
                    schedule_delay_millis=500,
                    max_export_batch_size=256,
                    export_timeout_millis=2000,
                )
            )
        _provider = provider
        return provider


def get_tracer(name: str = "my-agent") -> trace.Tracer:
    provider = _provider
    if provider is not None:
        return provider.get_tracer(name)
    return trace.get_tracer(name)


@contextmanager
def start_span(
    name: str,
    *,
    attributes: Optional[dict[str, Any]] = None,
    parent_context: Optional[Context] = None,
    start_time: Optional[int] = None,
    root: bool = False,
) -> Iterator[Span]:
    context = Context() if root else parent_context
    safe_attributes = normalize_attributes(attributes or {})
    with get_tracer().start_as_current_span(
        name,
        context=context,
        attributes=safe_attributes,
        start_time=start_time,
        record_exception=True,
        set_status_on_exception=True,
    ) as span:
        yield span


def add_event(
    span: Optional[Span],
    name: str,
    attributes: Optional[dict[str, Any]] = None,
    *,
    timestamp: Optional[int] = None,
) -> None:
    target = span or trace.get_current_span()
    if not target.is_recording():
        return
    target.add_event(name, normalize_attributes(attributes or {}), timestamp=timestamp)


def add_content_event(span: Optional[Span], name: str, content: Any, **metadata: Any) -> None:
    if not _capture_content:
        return
    target = span or trace.get_current_span()
    if not target.is_recording():
        return
    redacted = redact_data(content)
    text = redacted if isinstance(redacted, str) else json.dumps(redacted, ensure_ascii=False, default=str)
    encoded = text.encode("utf-8", errors="replace")
    attrs: dict[str, Any] = {
        "content": text,
        "content.mime_type": "text/plain" if isinstance(redacted, str) else "application/json",
        "content.bytes": len(encoded),
        "content.sha256": hashlib.sha256(encoded).hexdigest(),
        **metadata,
    }
    if len(encoded) > _max_content_bytes:
        clipped = encoded[:_max_content_bytes].decode("utf-8", errors="ignore")
        attrs["content"] = clipped
        attrs["content.truncated"] = True
        attrs["content.original_bytes"] = len(encoded)
    target.add_event(name, normalize_attributes(attrs))


def set_attributes(span: Optional[Span], **attributes: Any) -> None:
    target = span or trace.get_current_span()
    if not target.is_recording():
        return
    for key, value in normalize_attributes(attributes).items():
        target.set_attribute(key, value)


def record_error(span: Optional[Span], error: BaseException, **attributes: Any) -> None:
    target = span or trace.get_current_span()
    if not target.is_recording():
        return
    target.record_exception(error, attributes=normalize_attributes(attributes))
    target.set_status(Status(StatusCode.ERROR, f"{type(error).__name__}: {error}"))


def set_error(span: Optional[Span], description: str) -> None:
    target = span or trace.get_current_span()
    if target.is_recording():
        target.set_status(Status(StatusCode.ERROR, description))


def current_context() -> Context:
    return otel_context.get_current()


def attach_context(context: Context):
    return otel_context.attach(context)


def detach_context(token: Any) -> None:
    otel_context.detach(token)


def trace_id_hex(span: Optional[Span] = None) -> str:
    target = span or trace.get_current_span()
    context = target.get_span_context()
    return f"{context.trace_id:032x}" if context.is_valid else ""


def stable_hash(value: Any) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def redact_data(value: Any, key: str = "") -> Any:
    if key and _sensitive_key.search(key):
        return REDACTED
    if isinstance(value, dict):
        return {str(k): redact_data(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_data(item) for item in value]
    if isinstance(value, str):
        return _openai_key.sub(REDACTED, _bearer.sub("Bearer " + REDACTED, value))
    return value


def normalize_attributes(attributes: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for raw_key, raw_value in attributes.items():
        if raw_value is None:
            continue
        key = str(raw_key)
        value = redact_data(raw_value, key)
        if isinstance(value, (str, bool, int, float)):
            result[key] = value
        elif isinstance(value, (list, tuple)) and all(
            isinstance(item, (str, bool, int, float)) for item in value
        ):
            result[key] = list(value)
        else:
            result[key] = json.dumps(value, ensure_ascii=False, default=str)
    return result


def shutdown_observability(timeout_millis: int = 2000) -> None:
    global _provider
    with _provider_lock:
        provider = _provider
        _provider = None
    if provider is None:
        return
    try:
        provider.force_flush(timeout_millis=timeout_millis)
    except Exception:
        LOGGER.debug("OTel force_flush failed", exc_info=True)
    try:
        provider.shutdown()
    except Exception:
        LOGGER.debug("OTel shutdown failed", exc_info=True)


def _service_version(project_root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=1,
            check=False,
        )
        value = result.stdout.strip()
        return value or "unknown"
    except Exception:
        return "unknown"
