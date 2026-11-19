import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from channel.target import ChannelTarget
from observability.correlation import scheduler_correlation_attributes
from scheduler.schedule import ScheduleError, calculate_next_run, parse_schedule
from scheduler.repository import SchedulerRepository
from tools.base import BaseTool, ToolError, ToolResult
from agent.run_context import RunContext

VALID_ACTIONS = ("create", "list", "get", "delete", "enable", "disable")
VALID_TIMING_SEMANTICS = ("notify_at", "start_at", "deliver_by")


class SchedulerTool(BaseTool):
    name = "scheduler"
    description = (
        "Create and manage scheduled tasks (reminders / recurring jobs). Use when the "
        "user asks to be reminded later or on a recurring schedule, e.g. 'remind me in "
        "10 minutes', 'every day at 9am summarize my report'. A background service fires "
        "tasks at their time and delivers them back to the user."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": list(VALID_ACTIONS),
                "description": "Operation to perform.",
            },
            "task_id": {
                "type": "string",
                "description": "Target task id, required for get/delete/enable/disable.",
            },
            "name": {
                "type": "string",
                "description": "Human-readable task name (create).",
            },
            "message": {
                "type": "string",
                "description": "Fixed reminder text to send at the scheduled time (create). One of message / ai_task / tool_name.",
            },
            "ai_task": {
                "type": "string",
                "description": "A task for the agent to perform at the scheduled time; its answer is delivered to the user (create).",
            },
            "tool_name": {
                "type": "string",
                "description": "Name of a tool to invoke directly at the scheduled time (create).",
            },
            "tool_arguments": {
                "type": "object",
                "description": "Arguments for tool_name when using a tool_call task (create).",
            },
            "schedule_type": {
                "type": "string",
                "enum": ["once", "interval", "cron"],
                "description": "Scheduling mode (create).",
            },
            "schedule_value": {
                "type": "string",
                "description": (
                    "once: '+10m' / '+1h' / '+30s' / '+2d' or an ISO datetime. "
                    "interval: integer seconds. cron: a 5-field cron expression like '0 9 * * *'."
                ),
            },
            "timing_semantics": {
                "type": "string",
                "enum": list(VALID_TIMING_SEMANTICS),
                "description": (
                    "notify_at sends a fixed reminder at the time; start_at begins work at the time; "
                    "deliver_by prepares early and releases the result at the time."
                ),
            },
            "min_lead_seconds": {
                "type": "number",
                "description": "Minimum preparation lead time for deliver_by (default 30 seconds).",
            },
            "max_lead_seconds": {
                "type": "number",
                "description": "Maximum preparation lead time for deliver_by (default 3600 seconds).",
            },
        },
        "required": ["action"],
    }
    risk_level = "R2"
    idempotent = False

    def __init__(self, workspace: Path, store: SchedulerRepository):
        super().__init__(workspace)
        self.store = store
        self.control: Optional[Any] = None

    def bind_control(self, control: Any) -> None:
        """Bind the live scheduler after ChannelManager has constructed it."""
        self.control = control

    def risk_for(self, arguments: dict[str, Any]) -> str:
        action = str((arguments or {}).get("action", "")).strip()
        if action in ("list", "get"):
            return "R0"
        return "R2"

    def run(
        self,
        arguments: dict[str, Any],
        context: Optional[RunContext] = None,
    ) -> ToolResult:
        try:
            content, metadata = self._dispatch(arguments, context)
            return ToolResult(ok=True, content=content, metadata=metadata)
        except (ToolError, ScheduleError) as e:
            return ToolResult(ok=False, content=str(e))
        except Exception as e:
            return ToolResult(ok=False, content=f"{type(e).__name__}: {e}")

    def _dispatch(
        self,
        arguments: dict[str, Any],
        context: Optional[RunContext],
    ) -> tuple[str, dict[str, Any]]:
        action = str(arguments.get("action", "")).strip()
        if action not in VALID_ACTIONS:
            raise ToolError(f"action must be one of {VALID_ACTIONS}, got {action!r}")

        if action == "create":
            content = self._create(arguments, context)
            payload = json.loads(content)
            task = self.store.get(str(payload.get("task_id") or "")) or {}
            validation = payload.get("validation") or {}
            run = {
                "id": validation.get("run_id"),
                "run_kind": "validation" if validation.get("run_id") else None,
            }
            return content, scheduler_correlation_attributes(
                operation="scheduler.create",
                task=task,
                run=run,
            )
        if action == "list":
            return self._list(), scheduler_correlation_attributes(operation="scheduler.list")
        task_id = self._require_task_id(arguments)
        if action == "get":
            content = self._get(arguments)
            task = self.store.get(task_id) or {"id": task_id}
            return content, scheduler_correlation_attributes(operation="scheduler.get", task=task)
        if action == "delete":
            task = self.store.get(task_id) or {"id": task_id}
            content = self._delete(arguments)
            return content, scheduler_correlation_attributes(operation="scheduler.delete", task=task)
        content = self._set_enabled(arguments, enabled=(action == "enable"))
        task = self.store.get(task_id) or {"id": task_id}
        return content, scheduler_correlation_attributes(operation=f"scheduler.{action}", task=task)

    def _create(self, arguments: dict[str, Any], context: Optional[RunContext]) -> str:
        if context is None:
            raise ToolError("scheduler.create requires an active run context")
        target = ChannelTarget.from_context(context)
        if target is None:
            raise ToolError("cannot determine delivery target from current context")

        action = self._build_action(arguments, target)
        schedule = parse_schedule(
            str(arguments.get("schedule_type", "")),
            str(arguments.get("schedule_value", "")),
        )

        now = datetime.now()
        next_run = calculate_next_run(schedule, from_time=now)
        if next_run is None:
            raise ToolError("schedule produces no future run time")

        task_id = self.store.new_task_id()
        timing_semantics = str(arguments.get("timing_semantics") or "").strip()
        if not timing_semantics:
            timing_semantics = "notify_at" if action["type"] == "send_message" else "start_at"
        if timing_semantics not in VALID_TIMING_SEMANTICS:
            raise ToolError(f"timing_semantics must be one of {VALID_TIMING_SEMANTICS}")
        min_lead = self._lead_bound(arguments, "min_lead_seconds", 30.0)
        max_lead = self._lead_bound(arguments, "max_lead_seconds", 3600.0)
        if min_lead > max_lead:
            raise ToolError("min_lead_seconds must be <= max_lead_seconds")
        validating = self.control is not None
        task = {
            "id": task_id,
            "name": str(arguments.get("name") or "").strip() or f"task-{task_id}",
            "enabled": not validating,
            "status": "validating" if validating else "enabled",
            "created_at": now.isoformat(timespec="seconds"),
            "updated_at": now.isoformat(timespec="seconds"),
            "schedule": schedule,
            "action": action,
            "timing_semantics": timing_semantics,
            "min_lead_seconds": min_lead,
            "max_lead_seconds": max_lead,
            "next_run_at": next_run.isoformat(),
            "last_run_at": None,
            "last_error": None,
            "last_error_at": None,
        }
        self.store.add(task)
        validation = None
        if validating:
            try:
                validation = self.control.validate_task(task_id)
            except Exception as exc:
                self.store.update(
                    task_id,
                    {
                        "status": "validation_failed",
                        "last_error": f"{type(exc).__name__}: {exc}",
                        "last_error_at": datetime.now().isoformat(timespec="seconds"),
                    },
                )
                raise ToolError(f"task saved but validation could not start: {exc}") from exc
        return json.dumps(
            {
                "ok": True,
                "task_id": task_id,
                "name": task["name"],
                "next_run_at": task["next_run_at"],
                "timing_semantics": timing_semantics,
                "validation": validation,
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _build_action(arguments: dict[str, Any], target: ChannelTarget) -> dict[str, Any]:
        message = (arguments.get("message") or "").strip()
        ai_task = (arguments.get("ai_task") or "").strip()
        tool_name = (arguments.get("tool_name") or "").strip()

        provided = [bool(message), bool(ai_task), bool(tool_name)]
        if sum(provided) != 1:
            raise ToolError("exactly one of message / ai_task / tool_name must be provided")

        target_dict = target.to_dict()
        if message:
            return {"type": "send_message", "content": message, "target": target_dict}
        if ai_task:
            return {"type": "agent_task", "ai_task": ai_task, "target": target_dict}
        return {
            "type": "tool_call",
            "tool_name": tool_name,
            "tool_arguments": arguments.get("tool_arguments") or {},
            "target": target_dict,
        }

    def _list(self) -> str:
        tasks = self.store.list()
        summary = [
            {
                "id": t["id"],
                "name": t.get("name"),
                "enabled": t.get("enabled", True),
                "type": (t.get("action") or {}).get("type"),
                "schedule": t.get("schedule"),
                "next_run_at": t.get("next_run_at"),
            }
            for t in tasks
        ]
        return json.dumps({"count": len(summary), "tasks": summary}, ensure_ascii=False, indent=2)

    def _get(self, arguments: dict[str, Any]) -> str:
        task = self.store.get(self._require_task_id(arguments))
        if task is None:
            raise ToolError(f"unknown task: {arguments.get('task_id')}")
        return json.dumps(task, ensure_ascii=False, indent=2)

    def _delete(self, arguments: dict[str, Any]) -> str:
        task_id = self._require_task_id(arguments)
        if self.control is not None:
            self.control.remove_task(task_id)
        if not self.store.delete(task_id):
            raise ToolError(f"unknown task: {task_id}")
        return json.dumps({"ok": True, "deleted": task_id}, ensure_ascii=False)

    def _set_enabled(self, arguments: dict[str, Any], enabled: bool) -> str:
        task_id = self._require_task_id(arguments)
        if self.store.get(task_id) is None:
            raise ToolError(f"unknown task: {task_id}")
        self.store.set_enabled(task_id, enabled)
        if self.control is not None:
            if enabled:
                self.control.sync_task(task_id)
            else:
                self.control.remove_task(task_id)
        return json.dumps({"ok": True, "task_id": task_id, "enabled": enabled}, ensure_ascii=False)

    @staticmethod
    def _lead_bound(arguments: dict[str, Any], key: str, default: float) -> float:
        raw = arguments.get(key, default)
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise ToolError(f"{key} must be a number") from exc
        if value < 0:
            raise ToolError(f"{key} must be >= 0")
        return value

    @staticmethod
    def _require_task_id(arguments: dict[str, Any]) -> str:
        task_id = str(arguments.get("task_id") or "").strip()
        if not task_id:
            raise ToolError("task_id is required")
        return task_id
