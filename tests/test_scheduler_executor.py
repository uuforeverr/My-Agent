from pathlib import Path

from agent.run_context import RunContext
from channel.target import ChannelTarget
from scheduler.executor import SchedulerExecutor
from tools.base import ToolResult


def _target_dict(channel="cli", user="local", session="cli:local"):
    return ChannelTarget(channel_type=channel, user_id=user, session_id=session).to_dict()


def _build_context(session_id, **metadata):
    return RunContext.create(session_id=session_id, workspace=Path("."), **metadata)


def test_send_message_delivers_content():
    sent = []
    executor = SchedulerExecutor(send_reply=lambda t, r: sent.append((t, r)) or True)
    task = {
        "id": "t1",
        "action": {"type": "send_message", "content": "该喝水了", "target": _target_dict()},
    }
    assert executor.execute(task) is True
    target, reply = sent[0]
    assert target.channel_type == "cli"
    assert reply.content == "该喝水了"


def test_send_message_failure_returns_false():
    executor = SchedulerExecutor(send_reply=lambda t, r: False)
    task = {"id": "t1", "action": {"type": "send_message", "content": "x", "target": _target_dict()}}
    assert executor.execute(task) is False


def test_agent_task_isolated_session_and_flag():
    captured = {}

    def run_agent(message, context=None):
        captured["message"] = message
        captured["session_id"] = context.session_id
        captured["is_scheduled_task"] = context.get("is_scheduled_task")
        return "daily summary", None

    sent = []
    executor = SchedulerExecutor(
        send_reply=lambda t, r: sent.append(r) or True,
        run_agent=run_agent,
        build_context=_build_context,
    )
    task = {
        "id": "abcd",
        "action": {"type": "agent_task", "ai_task": "summarize", "target": _target_dict()},
    }
    assert executor.execute(task) is True
    assert captured["message"] == "summarize"
    assert captured["session_id"] == "scheduler:cli:local:abcd"
    assert captured["is_scheduled_task"] is True
    assert sent[0].content == "daily summary"


def test_agent_task_empty_answer_still_success_no_send():
    sent = []
    executor = SchedulerExecutor(
        send_reply=lambda t, r: sent.append(r) or True,
        run_agent=lambda message, context=None: ("", None),
        build_context=_build_context,
    )
    task = {"id": "a", "action": {"type": "agent_task", "ai_task": "x", "target": _target_dict()}}
    assert executor.execute(task) is True
    assert sent == []


def test_tool_call_runs_tool_and_delivers_result():
    captured = {}

    def run_tool(name, arguments, context):
        captured["name"] = name
        captured["arguments"] = arguments
        captured["is_scheduled_task"] = context.get("is_scheduled_task")
        return ToolResult(ok=True, content="search results")

    sent = []
    executor = SchedulerExecutor(
        send_reply=lambda t, r: sent.append(r) or True,
        run_tool=run_tool,
        build_context=_build_context,
    )
    task = {
        "id": "tc",
        "action": {
            "type": "tool_call",
            "tool_name": "web_search",
            "tool_arguments": {"query": "news"},
            "target": _target_dict(),
        },
    }
    assert executor.execute(task) is True
    assert captured["name"] == "web_search"
    assert captured["arguments"] == {"query": "news"}
    assert captured["is_scheduled_task"] is True
    assert sent[0].content == "search results"


def test_memory_dream_runs_without_delivery_target():
    captured = {}

    def run_dream(lookback_days=1):
        captured["lookback_days"] = lookback_days
        return {"ok": True, "dream_path": "dreams/today.md"}

    sent = []
    executor = SchedulerExecutor(
        send_reply=lambda t, r: sent.append(r) or True,
        run_dream=run_dream,
    )
    task = {
        "id": "dream",
        "action": {"type": "memory_dream", "lookback_days": 3},
    }

    assert executor.execute(task) is True
    assert captured["lookback_days"] == 3
    assert sent == []


def test_memory_scan_episodic_runs_without_delivery_target():
    captured = {}

    def run_scan(keep_recent_turns=3, min_flush_turns=20):
        captured["keep_recent_turns"] = keep_recent_turns
        captured["min_flush_turns"] = min_flush_turns
        return {"ok": True, "skipped": False}

    sent = []
    executor = SchedulerExecutor(
        send_reply=lambda t, r: sent.append(r) or True,
        run_scan=run_scan,
    )
    task = {
        "id": "scan",
        "action": {
            "type": "memory_scan_episodic",
            "keep_recent_turns": 3,
            "min_flush_turns": 20,
        },
    }

    assert executor.execute(task) is True
    assert captured["keep_recent_turns"] == 3
    assert captured["min_flush_turns"] == 20
    assert sent == []


def test_memory_scan_episodic_requires_runner():
    executor = SchedulerExecutor(send_reply=lambda t, r: True)
    task = {"id": "scan", "action": {"type": "memory_scan_episodic"}}
    try:
        executor.execute(task)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_memory_dream_requires_runner():
    executor = SchedulerExecutor(send_reply=lambda t, r: True)
    task = {"id": "dream", "action": {"type": "memory_dream"}}
    try:
        executor.execute(task)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_missing_target_raises():
    executor = SchedulerExecutor(send_reply=lambda t, r: True)
    task = {"id": "x", "action": {"type": "send_message", "content": "hi", "target": {}}}
    try:
        executor.execute(task)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_unknown_action_type_raises():
    executor = SchedulerExecutor(send_reply=lambda t, r: True)
    task = {"id": "x", "action": {"type": "explode", "target": _target_dict()}}
    try:
        executor.execute(task)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_run_to_result_returns_scheduled_message_without_legacy_trace_store():
    executor = SchedulerExecutor(send_reply=lambda t, r: True)
    task = {
        "id": "task-correlated",
        "name": "correlated task",
        "timing_semantics": "notify_at",
        "next_run_at": "2026-10-14T13:00:00",
        "action": {
            "type": "send_message",
            "content": "hello",
            "target": _target_dict(session="cli:local:conversation"),
        },
    }

    result = executor.run_to_result(
        task,
        run_id="run-correlated",
        run_kind="scheduled",
        scheduled_for="2026-10-14T13:00:00",
        deliver_at="2026-10-14T13:00:00",
    )

    assert result.succeeded
    assert result.content == "hello"
