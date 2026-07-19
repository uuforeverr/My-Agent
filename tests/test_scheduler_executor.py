import json
from pathlib import Path

from agent.run_context import RunContext
from channel.target import ChannelTarget
from observability import JsonlTraceStore
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


def test_executor_persists_send_message_trace(tmp_path):
    trace_path = tmp_path / "traces.jsonl"
    executor = SchedulerExecutor(
        send_reply=lambda t, r: True,
        trace_store=JsonlTraceStore(trace_path),
    )
    task = {
        "id": "t1",
        "action": {"type": "send_message", "content": "该喝水了", "target": _target_dict()},
    }

    assert executor.execute(task) is True

    records = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 1
    assert records[0]["status"] == "success"
    assert records[0]["root_span_id"] == records[0]["spans"][0]["span_id"]
    assert records[0]["spans"][0]["span_kind"] == "scheduler.execute"
    assert records[0]["spans"][0]["attributes"]["delivered"] is True
    assert [event["name"] for event in records[0]["events"]] == [
        "scheduler_execute_start",
        "scheduler_execute_completed",
    ]
    assert records[0]["events"][0]["task_id"] == "t1"


def test_executor_persists_error_trace(tmp_path):
    trace_path = tmp_path / "traces.jsonl"
    executor = SchedulerExecutor(
        send_reply=lambda t, r: True,
        trace_store=JsonlTraceStore(trace_path),
    )
    task = {"id": "x", "action": {"type": "explode", "target": _target_dict()}}

    try:
        executor.execute(task)
        assert False, "expected ValueError"
    except ValueError:
        pass

    record = json.loads(trace_path.read_text(encoding="utf-8").splitlines()[0])
    assert record["status"] == "error"
    assert record["error"]["type"] == "ValueError"
    assert record["spans"][0]["span_kind"] == "scheduler.execute"
    assert record["spans"][0]["status"] == "error"
    assert record["events"][-1]["name"] == "scheduler_execute_error"


def test_agent_task_reuses_scheduler_trace():
    captured = {}

    def run_agent(message, context=None):
        captured["trace"] = context.trace
        context.trace.add("agent_inner_event")
        return "done", context.trace

    executor = SchedulerExecutor(
        send_reply=lambda t, r: True,
        run_agent=run_agent,
        build_context=_build_context,
    )
    task = {
        "id": "abcd",
        "action": {"type": "agent_task", "ai_task": "summarize", "target": _target_dict()},
    }

    assert executor.execute(task) is True
    assert captured["trace"].events[1]["name"] == "agent_inner_event"


def test_agent_task_span_is_child_of_scheduler_span():
    captured = {}

    def run_agent(message, context=None):
        captured["trace"] = context.trace
        with context.trace.start_span("agent.invoke", span_kind="agent.invoke") as span:
            captured["agent_span_id"] = span.span_id
            captured["agent_parent_span_id"] = span.parent_span_id
        return "done", context.trace

    executor = SchedulerExecutor(
        send_reply=lambda t, r: True,
        run_agent=run_agent,
        build_context=_build_context,
    )
    task = {
        "id": "abcd",
        "action": {"type": "agent_task", "ai_task": "summarize", "target": _target_dict()},
    }

    assert executor.execute(task) is True
    scheduler_span = next(span for span in captured["trace"].spans if span.span_kind == "scheduler.execute")
    assert captured["agent_parent_span_id"] == scheduler_span.span_id
