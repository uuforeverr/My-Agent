import json
import threading
import time
from pathlib import Path

from agent.agent import Agent
from agent.run_context import RunContext
from memory.manager import MemoryManager
from scheduler.task_store import TaskStore
from tools.approval import (
    ApprovalGate,
    format_approval_prompt,
    handle_approval_command,
    parse_approval_command,
)
from tools.base import BaseTool
from tools.bash_tool import BashTool
from tools.policy import ToolRequest
from tools.registry import ToolRegistry
from tools.sandbox import SandboxResult


class _SpyTool(BaseTool):
    def __init__(self, workspace: Path, name: str, risk_level: str, requires_approval=None):
        super().__init__(workspace)
        self.name = name
        self.risk_level = risk_level
        self.requires_approval = requires_approval
        self.calls = []

    def execute(self, arguments):
        self.calls.append(dict(arguments))
        return f"ran:{self.name}"


def _memory(tmp_memory_dir):
    return MemoryManager(tmp_memory_dir, llm=None)


def _registry(tmp_workspace, memory, store, mode="always", gate=None):
    registry = ToolRegistry(
        tmp_workspace,
        memory,
        scheduler_store=store,
        approval_mode=mode,
        approval_gate=gate,
    )
    return registry


def _wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _run_in_thread(fn):
    box = {}

    def wrapper():
        try:
            box["result"] = fn()
        except Exception as exc:
            box["error"] = exc

    thread = threading.Thread(target=wrapper)
    thread.start()
    return thread, box


def test_parse_approval_command():
    assert parse_approval_command("hello") is None
    assert parse_approval_command("/summarize") is None
    assert parse_approval_command("/approve") == ("invalid", "", "")
    assert parse_approval_command("/approve a3f9c1d2") == ("approve", "a3f9c1d2", "once")
    assert parse_approval_command("/Approve A3F9C1D2 session") == ("approve", "A3F9C1D2", "session")
    assert parse_approval_command("/deny a3f9c1d2 extra") == ("deny", "a3f9c1d2", "")


def test_approval_prompt_includes_name_args_and_commands():
    request = ToolRequest(
        name="bash",
        arguments={"command": "pytest tests/test_bash_tool.py", "timeout": 20},
        risk_level="L2",
    )
    prompt = format_approval_prompt("a3f9c1d2", request, timeout_seconds=120)

    assert "待审批 [a3f9c1d2]" in prompt
    assert "工具: bash" in prompt
    assert "等级: L2" in prompt
    assert "command: pytest tests/test_bash_tool.py" in prompt
    assert "timeout: 20" in prompt
    assert "/approve a3f9c1d2          仅本次" in prompt
    assert "/approve a3f9c1d2 session" not in prompt
    assert "/deny a3f9c1d2             拒绝" in prompt
    assert "超时 120s 将自动拒绝。" in prompt


def test_approval_prompt_redacts_sensitive_fields_and_truncates_long_text():
    long_content = "x" * 250
    request = ToolRequest(
        name="write_file",
        arguments={
            "path": "app.env",
            "content": long_content,
            "api_key": "sk-live-please-hide",
            "timeout": 20,
            "token": "abc",
            "note": "password=hunter2",
        },
        risk_level="L1",
    )
    prompt = format_approval_prompt("deadbeef", request, timeout_seconds=120)

    assert "path: app.env" in prompt
    assert "api_key: [redacted]" in prompt
    assert "token: [redacted]" in prompt
    assert "note: [redacted]" in prompt
    assert "sk-live-please-hide" not in prompt
    assert "hunter2" not in prompt
    assert "timeout: 20" in prompt
    assert long_content not in prompt
    assert f"content: {'x' * 200}（共 250 字）" in prompt


def test_timeout_rejects_without_running_tool(tmp_workspace, tmp_memory_dir, tmp_path):
    memory = _memory(tmp_memory_dir)
    store = TaskStore(tmp_path / "tasks.json")
    gate = ApprovalGate(timeout_seconds=0.05)
    registry = _registry(tmp_workspace, memory, store, gate=gate)
    spy = _SpyTool(tmp_workspace, "bash", "L2")
    registry.tools["bash"] = spy
    events = []
    context = RunContext.create(
        session_id="cli:alice",
        workspace=tmp_workspace,
        channel_type="cli",
        user_id="alice",
        on_event=events.append,
    )

    started = time.monotonic()
    result = registry.run("bash", {"command": "python -c pass"}, context)
    elapsed = time.monotonic() - started

    assert result.ok is False
    assert "timed out" in result.content
    assert spy.calls == []
    assert elapsed < 1.5
    assert events and events[0]["type"] == "approval_request"
    assert "bash" in events[0]["content"]
    memory.close()


def test_approve_once_runs_tool(tmp_workspace, tmp_memory_dir, tmp_path, otel_exporter):
    memory = _memory(tmp_memory_dir)
    store = TaskStore(tmp_path / "tasks.json")
    gate = ApprovalGate(timeout_seconds=2)
    registry = _registry(tmp_workspace, memory, store, gate=gate)
    spy = _SpyTool(tmp_workspace, "bash", "L2")
    registry.tools["bash"] = spy
    events = []
    context = RunContext.create(
        session_id="cli:alice",
        workspace=tmp_workspace,
        channel_type="cli",
        user_id="alice",
        on_event=events.append,
    )

    thread, box = _run_in_thread(
        lambda: registry.run("bash", {"command": "python -c pass"}, context)
    )
    assert _wait_until(lambda: bool(events))
    approval_id = events[0]["approval_id"]
    ok, message = gate.approve(approval_id, "alice", "once")
    thread.join(timeout=2)

    assert ok is True
    assert "已批准" in message
    assert box["result"].ok is True
    assert spy.calls == [{"command": "python -c pass"}]
    spans = otel_exporter.get_finished_spans()
    policy_span = next(span for span in spans if span.name == "tool.policy")
    execute_span = next(span for span in spans if span.name == "execute_tool bash")
    assert policy_span.parent.span_id == execute_span.context.span_id
    assert policy_span.attributes["my_agent.policy.decision"] == "allow"
    assert policy_span.attributes["my_agent.approval.id"] == approval_id
    assert policy_span.attributes["my_agent.approval.scope"] == "once"
    assert execute_span.attributes.get("error.type") in (None, "")
    memory.close()


def test_deny_does_not_run_tool(tmp_workspace, tmp_memory_dir, tmp_path):
    memory = _memory(tmp_memory_dir)
    store = TaskStore(tmp_path / "tasks.json")
    gate = ApprovalGate(timeout_seconds=2)
    registry = _registry(tmp_workspace, memory, store, gate=gate)
    spy = _SpyTool(tmp_workspace, "bash", "L2")
    registry.tools["bash"] = spy
    events = []
    context = RunContext.create(
        session_id="cli:alice",
        workspace=tmp_workspace,
        user_id="alice",
        on_event=events.append,
    )

    thread, box = _run_in_thread(
        lambda: registry.run("bash", {"command": "python -c pass"}, context)
    )
    assert _wait_until(lambda: bool(events))
    ok, message = gate.deny(events[0]["approval_id"], "alice")
    thread.join(timeout=2)

    assert ok is True
    assert "已拒绝" in message
    assert box["result"].ok is False
    assert "denied" in box["result"].content
    assert spy.calls == []
    memory.close()


def test_wrong_user_cannot_approve(tmp_workspace, tmp_memory_dir, tmp_path):
    memory = _memory(tmp_memory_dir)
    store = TaskStore(tmp_path / "tasks.json")
    gate = ApprovalGate(timeout_seconds=2)
    request = ToolRequest(
        name="bash",
        arguments={"command": "python -c pass"},
        risk_level="L2",
        user_id="ou_owner",
        session_id="feishu:oc_group",
    )
    pending = gate.submit(request)

    ok, message = gate.approve(pending.approval_id, "ou_other", "once")

    assert ok is False
    assert "无权" in message
    assert gate.pending_ids() == [pending.approval_id]
    memory.close()


def test_bash_session_scope_is_downgraded_and_second_call_prompts(
    tmp_workspace, tmp_memory_dir, tmp_path
):
    memory = _memory(tmp_memory_dir)
    store = TaskStore(tmp_path / "tasks.json")
    gate = ApprovalGate(timeout_seconds=2)
    registry = _registry(tmp_workspace, memory, store, gate=gate)
    spy = _SpyTool(tmp_workspace, "bash", "L2")
    registry.tools["bash"] = spy
    events = []
    context = RunContext.create(
        session_id="cli:alice",
        workspace=tmp_workspace,
        user_id="alice",
        on_event=events.append,
    )

    thread, box = _run_in_thread(
        lambda: registry.run("bash", {"command": "python -c pass"}, context)
    )
    assert _wait_until(lambda: bool(events))
    gate.approve(events[0]["approval_id"], "alice", "session")
    thread.join(timeout=2)
    assert box["result"].ok is True

    second_thread, second_box = _run_in_thread(
        lambda: registry.run("bash", {"command": "python -c print(1)"}, context)
    )
    assert _wait_until(lambda: len(events) == 2)
    gate.approve(events[1]["approval_id"], "alice", "once")
    second_thread.join(timeout=2)
    second = second_box["result"]

    assert second.ok is True
    assert spy.calls == [
        {"command": "python -c pass"},
        {"command": "python -c print(1)"},
    ]
    assert len(events) == 2
    assert gate.is_session_allowed("cli:alice", "bash") is False
    memory.close()


def test_all_r2_tools_downgrade_session_approval_to_once():
    gate = ApprovalGate(timeout_seconds=2)
    pending = gate.submit(
        ToolRequest(
            name="scheduler",
            arguments={"action": "create"},
            risk_level="R2",
            session_id="cli:alice",
            user_id="alice",
        )
    )

    ok, message = gate.approve(pending.approval_id, "alice", "session")

    assert ok is True
    assert "仅本次" in message
    assert pending.scope == "once"
    assert gate.is_session_allowed("cli:alice", "scheduler") is False


def test_risk_based_approved_bash_still_uses_same_sandbox_and_hard_denies(
    tmp_workspace, tmp_memory_dir, tmp_path
):
    class FakeSandbox:
        def __init__(self):
            self.calls = []

        def execute(self, command, *, profile, timeout):
            self.calls.append((command, profile, timeout))
            return SandboxResult(0, "ok", "")

    memory = _memory(tmp_memory_dir)
    gate = ApprovalGate(timeout_seconds=2)
    registry = _registry(
        tmp_workspace,
        memory,
        TaskStore(tmp_path / "tasks.json"),
        mode="risk_based",
        gate=gate,
    )
    sandbox = FakeSandbox()
    registry.tools["bash"] = BashTool(tmp_workspace, executor=sandbox)
    events = []
    context = RunContext.create(
        session_id="cli:alice",
        workspace=tmp_workspace,
        user_id="alice",
        on_event=events.append,
    )

    thread, box = _run_in_thread(
        lambda: registry.run(
            "bash",
            {"command": "touch approved.txt", "profile": "workspace_write"},
            context,
        )
    )
    assert _wait_until(lambda: len(events) == 1)
    assert events[0]["session_scope_allowed"] is False
    gate.approve(events[0]["approval_id"], "alice", "session")
    thread.join(timeout=2)

    assert box["result"].ok is True
    assert sandbox.calls == [("touch approved.txt", "workspace_write", 20)]

    events.clear()
    denied_thread, denied_box = _run_in_thread(
        lambda: registry.run(
            "bash",
            {"command": "cat .env", "profile": "workspace_write"},
            context,
        )
    )
    assert _wait_until(lambda: len(events) == 1)
    gate.approve(events[0]["approval_id"], "alice", "once")
    denied_thread.join(timeout=2)

    assert denied_box["result"].ok is False
    assert "credential" in denied_box["result"].content.lower()
    assert len(sandbox.calls) == 1
    memory.close()


def test_never_mode_does_not_ask(tmp_workspace, tmp_memory_dir, tmp_path):
    memory = _memory(tmp_memory_dir)
    store = TaskStore(tmp_path / "tasks.json")
    gate = ApprovalGate(timeout_seconds=2)
    registry = _registry(tmp_workspace, memory, store, mode="never", gate=gate)
    web = _SpyTool(tmp_workspace, "web_search", "L2", requires_approval=False)
    registry.tools["web_search"] = web
    events = []
    context = RunContext.create(
        session_id="cli:alice",
        workspace=tmp_workspace,
        user_id="alice",
        on_event=events.append,
    )

    glob_result = registry.run("glob", {"pattern": "*"}, context)
    write_result = registry.run("write_file", {"path": "a.txt", "content": "hello"}, context)
    web_result = registry.run("web_search", {"query": "news"}, context)

    assert glob_result.ok is True
    assert write_result.ok is True
    assert web_result.ok is True
    assert events == []
    assert gate.pending_ids() == []
    memory.close()


def test_scheduled_l2_with_gate_parks_without_waiting(
    tmp_workspace, tmp_memory_dir, tmp_path
):
    memory = _memory(tmp_memory_dir)
    store = TaskStore(tmp_path / "tasks.json")
    gate = ApprovalGate(timeout_seconds=5)
    registry = _registry(tmp_workspace, memory, store, gate=gate)
    spy = _SpyTool(tmp_workspace, "bash", "L2")
    registry.tools["bash"] = spy
    context = RunContext.create(
        session_id="scheduler:cli:local:abcd1234",
        workspace=tmp_workspace,
        is_scheduled_task=True,
        task_id="abcd1234",
        user_id="local",
        channel_type="cli",
    )

    started = time.monotonic()
    result = registry.run("bash", {"command": "python -c pass"}, context)
    elapsed = time.monotonic() - started

    assert result.ok is False
    assert result.waiting_approval is True
    assert result.approval_id
    assert "待审批" in result.content
    assert "bash" in result.content
    assert "超时 24h 将自动拒绝" in result.content
    assert spy.calls == []
    assert gate.pending_ids() == [result.approval_id]
    assert elapsed < 1.0
    pending = gate.get(result.approval_id)
    assert pending.kind == "scheduled"
    memory.close()


def test_l1_write_and_edit_preview_go_to_trace_not_channel(
    tmp_workspace, tmp_memory_dir, tmp_path, otel_exporter
):
    memory = _memory(tmp_memory_dir)
    store = TaskStore(tmp_path / "tasks.json")
    registry = _registry(
        tmp_workspace,
        memory,
        store,
        mode="never",
        gate=ApprovalGate(timeout_seconds=1),
    )
    events = []
    context = RunContext.create(
        session_id="cli:alice",
        workspace=tmp_workspace,
        user_id="alice",
        on_event=events.append,
    )

    written = registry.run("write_file", {"path": "a.txt", "content": "hello"}, context)
    (tmp_workspace / "b.txt").write_text("old\n", encoding="utf-8")
    edited = registry.run(
        "edit_file",
        {"path": "b.txt", "old_text": "old", "new_text": "new"},
        context,
    )

    assert written.ok is True
    assert edited.ok is True
    assert events == []
    spans = otel_exporter.get_finished_spans()
    write_execute = next(span for span in spans if span.name == "execute_tool write_file")
    edit_execute = next(span for span in spans if span.name == "execute_tool edit_file")
    write_policy = next(
        span for span in spans
        if span.name == "tool.policy" and span.parent.span_id == write_execute.context.span_id
    )
    edit_policy = next(
        span for span in spans
        if span.name == "tool.policy" and span.parent.span_id == edit_execute.context.span_id
    )
    write_preview = json.loads(next(
        event.attributes["content"]
        for event in write_policy.events
        if event.name == "my_agent.tool.preview"
    ))
    assert write_preview["path"] == "a.txt"
    assert write_preview["bytes"] == 5
    assert write_preview["new_file"] is True
    edit_preview = json.loads(next(
        event.attributes["content"]
        for event in edit_policy.events
        if event.name == "my_agent.tool.preview"
    ))
    assert edit_preview["path"] == "b.txt"
    assert "-old" in edit_preview["diff_preview"]
    assert "+new" in edit_preview["diff_preview"]
    memory.close()


def test_handle_approval_command_does_not_need_agent_run():
    class StubAgent:
        def __init__(self):
            self.run_called = False
            self.calls = []

        def approve_tool(self, approval_id, user_id, scope="once"):
            self.calls.append(("approve", approval_id, user_id, scope))
            return f"已批准 [{approval_id}]"

        def deny_tool(self, approval_id, user_id):
            self.calls.append(("deny", approval_id, user_id))
            return f"已拒绝 [{approval_id}]"

        def run(self, *args, **kwargs):
            self.run_called = True
            raise AssertionError("slash approval must not enter Agent.run")

    agent = StubAgent()
    assert handle_approval_command(agent, "hello", "local") is None
    assert handle_approval_command(agent, "/approve abcdef12 session", "local") == "已批准 [abcdef12]"
    assert handle_approval_command(agent, "/deny abcdef12", "local") == "已拒绝 [abcdef12]"
    assert "用法" in handle_approval_command(agent, "/approve", "local")
    assert agent.calls == [
        ("approve", "abcdef12", "local", "session"),
        ("deny", "abcdef12", "local"),
    ]
    assert agent.run_called is False


def test_agent_installs_approval_gate_and_approve_methods(settings_factory, tmp_path):
    (tmp_path / "prompts").mkdir(parents=True, exist_ok=True)
    (tmp_path / "prompts" / "identity.md").write_text("You are test agent.", encoding="utf-8")
    (tmp_path / "skills").mkdir(parents=True, exist_ok=True)
    settings = settings_factory(project_root=tmp_path, workspace=tmp_path / "workspace")
    agent = Agent(settings)
    try:
        assert agent.tools.approval_gate is not None
        pending = agent.tools.approval_gate.submit(
            ToolRequest(name="bash", arguments={"command": "x"}, risk_level="L2", user_id="local")
        )
        assert "已批准" in agent.approve_tool(pending.approval_id, "local", "once")
        assert "未找到" in agent.deny_tool("missing01", "local")
    finally:
        agent.memory.close()
