import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from channel.target import ChannelTarget
from scheduler.schedule import ScheduleError, calculate_next_run, parse_schedule
from scheduler.task_store import TaskStore
from tools.base import BaseTool, ToolError, ToolResult
from agent.run_context import RunContext

VALID_ACTIONS = ("create", "list", "get", "delete", "enable", "disable")


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
        },
        "required": ["action"],
    }
    risk_level = "L2"
    idempotent = False

    def __init__(self, workspace: Path, store: TaskStore):
        super().__init__(workspace)
        self.store = store

    def risk_for(self, arguments: dict[str, Any]) -> str:
        action = str((arguments or {}).get("action", "")).strip()
        if action in ("list", "get"):
            return "L0"
        return "L2"

    def run(
        self,
        arguments: dict[str, Any],
        context: Optional[RunContext] = None,
    ) -> ToolResult:
        try:
            return ToolResult(ok=True, content=self._dispatch(arguments, context))
        except (ToolError, ScheduleError) as e:
            return ToolResult(ok=False, content=str(e))
        except Exception as e:
            return ToolResult(ok=False, content=f"{type(e).__name__}: {e}")

    def _dispatch(self, arguments: dict[str, Any], context: Optional[RunContext]) -> str:
        action = str(arguments.get("action", "")).strip()
        if action not in VALID_ACTIONS:
            raise ToolError(f"action must be one of {VALID_ACTIONS}, got {action!r}")

        if action == "create":
            return self._create(arguments, context)
        if action == "list":
            return self._list()
        if action == "get":
            return self._get(arguments)
        if action == "delete":
            return self._delete(arguments)
        return self._set_enabled(arguments, enabled=(action == "enable"))

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
        task = {
            "id": task_id,
            "name": str(arguments.get("name") or "").strip() or f"task-{task_id}",
            "enabled": True,
            "created_at": now.isoformat(timespec="seconds"),
            "updated_at": now.isoformat(timespec="seconds"),
            "schedule": schedule,
            "action": action,
            "next_run_at": next_run.isoformat(),
            "last_run_at": None,
            "last_error": None,
            "last_error_at": None,
        }
        self.store.add(task)
        return json.dumps(
            {
                "ok": True,
                "task_id": task_id,
                "name": task["name"],
                "next_run_at": task["next_run_at"],
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
        if not self.store.delete(task_id):
            raise ToolError(f"unknown task: {task_id}")
        return json.dumps({"ok": True, "deleted": task_id}, ensure_ascii=False)

    def _set_enabled(self, arguments: dict[str, Any], enabled: bool) -> str:
        task_id = self._require_task_id(arguments)
        if self.store.get(task_id) is None:
            raise ToolError(f"unknown task: {task_id}")
        self.store.set_enabled(task_id, enabled)
        return json.dumps({"ok": True, "task_id": task_id, "enabled": enabled}, ensure_ascii=False)

    @staticmethod
    def _require_task_id(arguments: dict[str, Any]) -> str:
        task_id = str(arguments.get("task_id") or "").strip()
        if not task_id:
            raise ToolError("task_id is required")
        return task_id
