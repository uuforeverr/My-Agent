import json
from datetime import datetime
from pathlib import Path

import pytest

from agent.run_context import RunContext
from scheduler.task_store import TaskStore
from tools.scheduler_tool import SchedulerTool


def _tool(tmp_path) -> SchedulerTool:
    store = TaskStore(tmp_path / "scheduler" / "tasks.json")
    return SchedulerTool(tmp_path / "workspace", store)


def _ctx(workspace, channel="cli", user="local", session="cli:local", **extra):
    return RunContext.create(
        session_id=session,
        workspace=Path(workspace),
        channel_type=channel,
        user_id=user,
        **extra,
    )


def test_create_send_message_snapshots_target(tmp_path):
    tool = _tool(tmp_path)
    ctx = _ctx(tmp_path / "workspace")
    result = tool.run(
        {
            "action": "create",
            "name": "drink water",
            "message": "该喝水了",
            "schedule_type": "once",
            "schedule_value": "+10m",
        },
        ctx,
    )
    assert result.ok is True
    payload = json.loads(result.content)
    task = tool.store.get(payload["task_id"])
    assert task["action"]["type"] == "send_message"
    assert task["action"]["content"] == "该喝水了"
    assert task["action"]["target"]["channel_type"] == "cli"
    assert task["action"]["target"]["user_id"] == "local"
    assert task["schedule"] == {"type": "once", "run_at": task["schedule"]["run_at"]}
    # +10m should be ~10 minutes out
    run_at = datetime.fromisoformat(task["next_run_at"])
    assert (run_at - datetime.now()).total_seconds() > 500


def test_create_requires_exactly_one_action_kind(tmp_path):
    tool = _tool(tmp_path)
    ctx = _ctx(tmp_path / "workspace")
    both = tool.run(
        {
            "action": "create",
            "message": "x",
            "ai_task": "y",
            "schedule_type": "interval",
            "schedule_value": "60",
        },
        ctx,
    )
    assert both.ok is False
    assert "exactly one" in both.content

    none = tool.run(
        {"action": "create", "schedule_type": "interval", "schedule_value": "60"},
        ctx,
    )
    assert none.ok is False


def test_create_missing_schedule_errors(tmp_path):
    tool = _tool(tmp_path)
    ctx = _ctx(tmp_path / "workspace")
    result = tool.run({"action": "create", "message": "hi"}, ctx)
    assert result.ok is False


def test_create_without_target_context_errors(tmp_path):
    tool = _tool(tmp_path)
    ctx = RunContext.create(session_id="default", workspace=tmp_path)
    result = tool.run(
        {
            "action": "create",
            "message": "hi",
            "schedule_type": "interval",
            "schedule_value": "60",
        },
        ctx,
    )
    assert result.ok is False
    assert "delivery target" in result.content


def test_create_agent_task_and_tool_call(tmp_path):
    tool = _tool(tmp_path)
    ctx = _ctx(tmp_path / "workspace")

    ai = tool.run(
        {
            "action": "create",
            "ai_task": "summarize my day",
            "schedule_type": "cron",
            "schedule_value": "0 9 * * *",
        },
        ctx,
    )
    if not ai.ok:
        pytest.skip("croniter not installed")
    ai_task = tool.store.get(json.loads(ai.content)["task_id"])
    assert ai_task["action"]["type"] == "agent_task"
    assert ai_task["action"]["ai_task"] == "summarize my day"

    tc = tool.run(
        {
            "action": "create",
            "tool_name": "web_search",
            "tool_arguments": {"query": "news"},
            "schedule_type": "interval",
            "schedule_value": "3600",
        },
        ctx,
    )
    tc_task = tool.store.get(json.loads(tc.content)["task_id"])
    assert tc_task["action"]["type"] == "tool_call"
    assert tc_task["action"]["tool_name"] == "web_search"
    assert tc_task["action"]["tool_arguments"] == {"query": "news"}


def test_list_get_delete_enable_disable(tmp_path):
    tool = _tool(tmp_path)
    ctx = _ctx(tmp_path / "workspace")
    created = tool.run(
        {
            "action": "create",
            "name": "job",
            "message": "hi",
            "schedule_type": "interval",
            "schedule_value": "60",
        },
        ctx,
    )
    task_id = json.loads(created.content)["task_id"]

    listing = json.loads(tool.run({"action": "list"}, ctx).content)
    assert listing["count"] == 1
    assert listing["tasks"][0]["id"] == task_id

    got = tool.run({"action": "get", "task_id": task_id}, ctx)
    assert json.loads(got.content)["id"] == task_id

    disabled = tool.run({"action": "disable", "task_id": task_id}, ctx)
    assert json.loads(disabled.content)["enabled"] is False
    assert tool.store.get(task_id)["enabled"] is False

    enabled = tool.run({"action": "enable", "task_id": task_id}, ctx)
    assert json.loads(enabled.content)["enabled"] is True

    deleted = tool.run({"action": "delete", "task_id": task_id}, ctx)
    assert json.loads(deleted.content)["deleted"] == task_id
    assert tool.store.get(task_id) is None


def test_get_unknown_task_errors(tmp_path):
    tool = _tool(tmp_path)
    ctx = _ctx(tmp_path / "workspace")
    assert tool.run({"action": "get", "task_id": "nope"}, ctx).ok is False
    assert tool.run({"action": "delete", "task_id": "nope"}, ctx).ok is False
    assert tool.run({"action": "enable"}, ctx).ok is False  # missing task_id


def test_invalid_action_errors(tmp_path):
    tool = _tool(tmp_path)
    ctx = _ctx(tmp_path / "workspace")
    assert tool.run({"action": "frobnicate"}, ctx).ok is False
