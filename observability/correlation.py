"""Stable correlation attribute names shared by OTel spans and scheduler records."""

from __future__ import annotations

from typing import Any, Optional


TASK_ID_ATTR = "my_agent.task.id"
TASK_IDS_ATTR = "my_agent.task.ids"
RUN_ID_ATTR = "my_agent.run.id"
DELIVERY_ID_ATTR = "my_agent.delivery.id"
OPERATION_ATTR = "my_agent.operation"


def scheduler_correlation_attributes(
    *,
    operation: str,
    task: Optional[dict[str, Any]] = None,
    run: Optional[dict[str, Any]] = None,
    delivery: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Build flat, filter-friendly OpenTelemetry attributes."""
    task = task or {}
    run = run or {}
    delivery = delivery or {}
    action = task.get("action") or {}
    target = action.get("target") or {}

    values = {
        OPERATION_ATTR: operation,
        TASK_ID_ATTR: task.get("id") or run.get("task_id"),
        "my_agent.task.name": task.get("name"),
        "my_agent.task.timing_semantics": task.get("timing_semantics"),
        "my_agent.task.next_run_at": task.get("next_run_at"),
        "my_agent.origin.session_id": target.get("session_id"),
        RUN_ID_ATTR: run.get("id") or delivery.get("run_id"),
        "my_agent.run.kind": run.get("run_kind"),
        "my_agent.run.scheduled_for": run.get("scheduled_for"),
        "my_agent.run.deliver_at": run.get("deliver_at"),
        DELIVERY_ID_ATTR: delivery.get("id"),
        "my_agent.delivery.kind": delivery.get("delivery_kind"),
        "my_agent.delivery.status": delivery.get("status"),
        "my_agent.delivery.attempt": delivery.get("attempt"),
    }
    return {key: value for key, value in values.items() if value not in (None, "")}
