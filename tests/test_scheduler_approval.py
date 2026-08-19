import time
from datetime import datetime, timedelta
from pathlib import Path

from agent.agent import Agent
from agent.run_context import RunContext
from channel.manager import RESTART_APPROVAL_MESSAGE, ChannelManager
from channel.reply import Reply
from memory.manager import MemoryManager
from scheduler.executor import WAITING_APPROVAL, SchedulerExecutor
from scheduler.service import SchedulerService
from scheduler.task_store import TaskStore
from tests.conftest import FakeLLM
from tests.test_approval import _SpyTool
from tests.test_channel_manager import _FakeAgent
from tools.approval import ApprovalGate
from tools.policy import ToolRequest
from tools.registry import ToolRegistry


def _store(tmp_path) -> TaskStore:
    return TaskStore(tmp_path / "scheduler" / "tasks.json")


def _target():
    return {"channel_type": "cli", "user_id": "local", "session_id": "cli:local"}


def _base_task(store, schedule, next_run_at, action=None):
    return {
        "id": store.new_task_id(),
        "name": "t",
        "enabled": True,
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
        "schedule": schedule,
        "action": action or {
            "type": "send_message",
            "content": "hi",
            "target": _target(),
        },
        "next_run_at": next_run_at,
        "last_run_at": None,
        "last_error": None,
        "last_error_at": None,
    }


def _prepare_project(root: Path):
    (root / "prompts").mkdir(parents=True, exist_ok=True)
    (root / "prompts" / "identity.md").write_text("You are test agent.", encoding="utf-8")
    (root / "skills").mkdir(parents=True, exist_ok=True)


def _registry(tmp_workspace, tmp_memory_dir, mode="on", gate=None):
    memory = MemoryManager(tmp_memory_dir, llm=None)
    store = TaskStore(tmp_workspace.parent / "tasks.json")
    return memory, ToolRegistry(
        tmp_workspace,
        memory,
        scheduler_store=store,
        approval_mode=mode,
        approval_gate=gate,
    )


def test_waiting_task_is_skipped_and_not_retried(tmp_path):
    store = _store(tmp_path)
    now = datetime(2026, 1, 1, 12, 0, 0)
    due = (now - timedelta(seconds=5)).isoformat()
    task = _base_task(store, {"type": "interval", "seconds": 3600}, due)
    store.add(task)

    fired = []

    def callback(item):
        fired.append(item["id"])
        item["pending_approval_id"] = "deadbeef"
        item["parked_session_id"] = "scheduler:cli:local:deadbeef"
        return WAITING_APPROVAL

    service = SchedulerService(store, execute_callback=callback)
    service.check_and_execute(now=now)

    assert fired == [task["id"]]
    updated = store.get(task["id"])
    assert updated["run_state"] == "waiting_approval"
    assert updated["pending_approval_id"] == "deadbeef"
    assert updated["next_run_at"] == due

    service.check_and_execute(now=now)
    assert fired == [task["id"]]


def test_complete_waiting_deny_once_disables(tmp_path):
    store = _store(tmp_path)
    now = datetime(2026, 1, 1, 12, 0, 0)
    due = now.isoformat()
    task = _base_task(store, {"type": "once", "run_at": due}, due)
    task["run_state"] = "waiting_approval"
    store.add(task)

    service = SchedulerService(store, execute_callback=lambda t: True)
    service.complete_waiting(task["id"], outcome="denied")

    updated = store.get(task["id"])
    assert updated is not None
    assert updated["enabled"] is False
    assert updated["run_state"] is None
    assert updated["last_error"] == "approval_denied"


def test_complete_waiting_deny_interval_advances_keeps_error(tmp_path):
    store = _store(tmp_path)
    now = datetime(2026, 1, 1, 12, 0, 0)
    due = (now - timedelta(seconds=5)).isoformat()
    task = _base_task(store, {"type": "interval", "seconds": 3600}, due)
    task["run_state"] = "waiting_approval"
    store.add(task)

    service = SchedulerService(store, execute_callback=lambda t: True)
    service.complete_waiting(task["id"], outcome="timeout")

    updated = store.get(task["id"])
    assert updated["enabled"] is True
    assert updated["last_error"] == "approval_timeout"
    next_run = datetime.fromisoformat(updated["next_run_at"])
    assert next_run > now


def test_complete_waiting_approved_once_deletes(tmp_path):
    store = _store(tmp_path)
    now = datetime(2026, 1, 1, 12, 0, 0)
    due = now.isoformat()
    task = _base_task(store, {"type": "once", "run_at": due}, due)
    task["run_state"] = "waiting_approval"
    store.add(task)

    service = SchedulerService(store, execute_callback=lambda t: True)
    service.complete_waiting(task["id"], outcome="approved")
    assert store.get(task["id"]) is None


def test_scheduled_approve_uses_24h_timeout_not_120s():
    gate = ApprovalGate(timeout_seconds=0.05, scheduled_timeout_seconds=2)
    pending = gate.submit(
        ToolRequest(name="bash", arguments={"command": "x"}, risk_level="L2", user_id="alice"),
        kind="scheduled",
    )
    time.sleep(0.1)
    ok, message = gate.approve(pending.approval_id, "alice")
    assert ok is True
    assert "已批准" in message


def test_expire_scheduled_fires_timeout_callback():
    resolved = []
    gate = ApprovalGate(timeout_seconds=120, scheduled_timeout_seconds=0.05)
    gate.on_scheduled_resolved = lambda pending, outcome: resolved.append((pending.approval_id, outcome))
    pending = gate.submit(
        ToolRequest(name="bash", arguments={"command": "x"}, risk_level="L2", user_id="local"),
        kind="scheduled",
    )
    time.sleep(0.08)
    expired = gate.expire_scheduled()
    assert [item.approval_id for item in expired] == [pending.approval_id]
    assert resolved == [(pending.approval_id, "timeout")]


def test_tool_call_parks_then_resume_runs_tool(tmp_workspace, tmp_memory_dir):
    gate = ApprovalGate()
    memory, registry = _registry(tmp_workspace, tmp_memory_dir, gate=gate)
    spy = _SpyTool(tmp_workspace, "bash", "L2")
    registry.tools["bash"] = spy
    sent = []

    def build_context(session_id, **metadata):
        return RunContext.create(session_id=session_id, workspace=tmp_workspace, **metadata)

    executor = SchedulerExecutor(
        send_reply=lambda target, reply: sent.append(reply.content) or True,
        run_tool=registry.run,
        build_context=build_context,
    )
    task = {
        "id": "tc01",
        "action": {
            "type": "tool_call",
            "tool_name": "bash",
            "tool_arguments": {"command": "python -c pass"},
            "target": _target(),
        },
    }

    started = time.monotonic()
    assert executor.execute(task) == WAITING_APPROVAL
    assert time.monotonic() - started < 1.0
    assert spy.calls == []
    assert sent and "待审批" in sent[0]
    approval_id = task["pending_approval_id"]
    assert approval_id
    assert gate.get(approval_id).resume_kind == "tool_call"

    gate.approve(approval_id, "local")
    context = RunContext.create(
        session_id=task["parked_session_id"],
        workspace=tmp_workspace,
        is_scheduled_task=True,
        task_id="tc01",
        user_id="local",
        channel_type="cli",
        resume_approval_id=approval_id,
        resume_kind="tool_call",
    )
    result = registry.run("bash", {"command": "python -c pass"}, context)
    assert result.ok is True
    assert spy.calls == [{"command": "python -c pass"}]
    memory.close()


def test_agent_task_parks_then_approve_continues(settings_factory, tmp_path):
    _prepare_project(tmp_path)
    settings = settings_factory(
        project_root=tmp_path,
        workspace=tmp_path / "workspace",
        tool_approval_mode="on",
        max_steps=4,
    )
    agent = Agent(settings)
    spy = _SpyTool(settings.workspace, "bash", "L2")
    agent.tools.tools["bash"] = spy
    agent.llm = FakeLLM([
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "function": {"name": "bash", "arguments": '{"command":"python -c pass"}'},
                }
            ],
        },
        {"role": "assistant", "content": "已完成调度任务"},
    ])

    sent = []

    def build_context(session_id, **metadata):
        return RunContext.create(
            session_id=session_id,
            workspace=settings.workspace,
            **metadata,
        )

    executor = SchedulerExecutor(
        send_reply=lambda target, reply: sent.append(reply.content) or True,
        run_agent=agent.run,
        build_context=build_context,
    )
    task = {
        "id": "ag01",
        "action": {"type": "agent_task", "ai_task": "run tests", "target": _target()},
    }

    started = time.monotonic()
    assert executor.execute(task) == WAITING_APPROVAL
    assert time.monotonic() - started < 1.5
    assert spy.calls == []
    approval_id = task["pending_approval_id"]
    history = agent._get_context_manager(task["parked_session_id"]).history
    assert history[-1]["role"] == "assistant"
    assert "tool_calls" in history[-1]

    ok, _message = agent.tools.approval_gate.approve(approval_id, "local")
    assert ok is True
    answer, _trace = agent.resume_after_approval(approval_id)
    assert spy.calls == [{"command": "python -c pass"}]
    assert answer == "已完成调度任务"
    agent.memory.close()


def test_start_scheduler_sweeps_stale_waiting_once_task(settings_factory, tmp_path):
    store = TaskStore(tmp_path / "scheduler" / "tasks.json")
    now = datetime.now()
    task = _base_task(
        store,
        {"type": "once", "run_at": now.isoformat()},
        now.isoformat(),
        action={"type": "agent_task", "ai_task": "hi", "target": _target()},
    )
    task["run_state"] = "waiting_approval"
    task["pending_approval_id"] = "deadbeef"
    task["parked_session_id"] = "scheduler:cli:local:deadbeef"
    store.add(task)

    settings = settings_factory(workspace=tmp_path / "w")
    manager = ChannelManager(settings, agent=_FakeAgent(scheduler_store=store))
    sent = []

    class RecordingChannel:
        channel_type = "cli"

        def send(self, reply: Reply, message):
            sent.append(reply.content)

    manager._channel_map = {"cli": RecordingChannel()}
    try:
        manager.start_scheduler()
        updated = store.get(task["id"])
        assert updated["enabled"] is False
        assert updated["run_state"] is None
        assert updated["last_error"] == "approval_expired_restart"
        assert RESTART_APPROVAL_MESSAGE in sent
    finally:
        manager.shutdown()
