"""Native OpenTelemetry tracing for my-agent."""

from observability.otel import (
    REDACTED,
    add_content_event,
    add_event,
    configure_observability,
    current_context,
    get_tracer,
    record_error,
    redact_data,
    set_attributes,
    set_error,
    shutdown_observability,
    stable_hash,
    start_span,
    trace_id_hex,
)

__all__ = [
    "REDACTED",
    "add_content_event",
    "add_event",
    "configure_observability",
    "current_context",
    "get_tracer",
    "record_error",
    "redact_data",
    "set_attributes",
    "set_error",
    "shutdown_observability",
    "stable_hash",
    "start_span",
    "trace_id_hex",
]
