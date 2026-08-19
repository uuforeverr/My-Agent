from pathlib import Path
import json

import pytest

from agent.agent import Agent
from agent.run_context import RunContext
from memory.manager import MemoryManager
from scheduler.task_store import TaskStore
from tools.base import BaseTool
from tools.bash_tool import BashTool
from tools.file_tools import EditFileTool, GlobTool, ReadFileTool, WriteFileTool
from tools.memory_tools import ReadMemoryTool, RememberMemoryTool, SearchMemoryTool
from tools.policy import ToolPolicy, ToolRequest, normalize_approval_mode
from tools.registry import ToolRegistry
from tools.scheduler_tool import SchedulerTool
from tools.web_search_tool import WebSearchTool


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


class _CapturePolicy(ToolPolicy):
    def __init__(self, mode="off"):
        super().__init__(mode)
        self.requests = []

    def decide(self, request):
        self.requests.append(request)
        return super().decide(request)


@pytest.fixture
def memory(tmp_memory_dir):
    manager = MemoryManager(tmp_memory_dir, llm=None)
    try:
        yield manager
    finally:
        manager.close()


@pytest.fixture
def store(tmp_path):
    return TaskStore(tmp_path / "scheduler" / "tasks.json")


def _registry(tmp_workspace, memory, store, mode="off", policy=None):
    return ToolRegistry(
        tmp_workspace,
        memory,
        scheduler_store=store,
        approval_mode=mode,
        policy=policy,
    )


def _request(**overrides):
    payload = {
        "name": "bash",
        "arguments": {"command": "python -c pass"},
        "risk_level": "L2",
    }
    payload.update(overrides)
    return ToolRequest(**payload)


def test_normalize_approval_mode_only_on_is_enabled():
    assert normalize_approval_mode("on") == "on"
    assert normalize_approval_mode("ON") == "on"
    assert normalize_approval_mode(" off ") == "off"
    assert normalize_approval_mode("true") == "off"
    assert normalize_approval_mode("1") == "off"
    assert normalize_approval_mode("") == "off"
    assert ToolPolicy("ON").mode == "on"


def test_policy_off_allows_every_level():
    policy = ToolPolicy("off")
    for risk in ("L0", "L1", "L2", "L3", "L9"):
        decision = policy.decide(_request(risk_level=risk))
        assert decision.action == "allow"
        assert decision.reason == "approval_mode_off"


def test_policy_on_levels_and_overrides():
    policy = ToolPolicy("on")
    assert policy.decide(_request(name="glob", risk_level="L0")).action == "allow"
    assert policy.decide(_request(name="write_file", risk_level="L1")).action == "allow"
    bash = policy.decide(_request())
    assert bash.action == "need_approval"
    assert bash.reason == "risk_requires_approval"
    l3 = policy.decide(_request(name="future", risk_level="L3"))
    assert l3.action == "need_approval"
    web = policy.decide(_request(name="web_search", risk_level="L2", requires_approval=False))
    assert web.action == "allow"
    assert web.reason == "requires_approval_false"
    forced = policy.decide(_request(name="glob", risk_level="L0", requires_approval=True))
    assert forced.action == "need_approval"
    unknown = policy.decide(_request(risk_level="LX"))
    assert unknown.action == "deny"
    assert unknown.reason == "unknown_risk_level"


def test_static_risk_table():
    assert GlobTool.risk_level == "L0"
    assert ReadFileTool.risk_level == "L0"
    assert SearchMemoryTool.risk_level == "L0"
    assert ReadMemoryTool.risk_level == "L0"
    assert WriteFileTool.risk_level == "L1"
    assert EditFileTool.risk_level == "L1"
    assert RememberMemoryTool.risk_level == "L1"
    assert WriteFileTool.idempotent is False
    assert BashTool.risk_level == "L2"
    assert BashTool.idempotent is False
    assert WebSearchTool.risk_level == "L2"
    assert WebSearchTool.requires_approval is False
    assert SchedulerTool.risk_level == "L2"


def test_scheduler_risk_for_by_action(tmp_workspace, tmp_path):
    tool = SchedulerTool(tmp_workspace, TaskStore(tmp_path / "tasks.json"))
    assert tool.risk_for({"action": "list"}) == "L0"
    assert tool.risk_for({"action": "get"}) == "L0"
    assert tool.risk_for({"action": "create"}) == "L2"
    assert tool.risk_for({"action": "delete"}) == "L2"
    assert tool.risk_for({"action": "enable"}) == "L2"
    assert tool.risk_for({"action": "disable"}) == "L2"
    assert tool.risk_for({"action": "unknown"}) == "L2"
    assert tool.risk_for({}) == "L2"


def test_off_mode_still_executes_l2(tmp_workspace, memory, store):
    registry = _registry(tmp_workspace, memory, store, mode="off")
    spy = _SpyTool(tmp_workspace, "bash", "L2")
    registry.tools["bash"] = spy

    result = registry.run("bash", {"command": "python -c pass"})

    assert result.ok is True
    assert spy.calls == [{"command": "python -c pass"}]


def test_on_mode_does_not_execute_l2_without_gate(tmp_workspace, memory, store):
    registry = _registry(tmp_workspace, memory, store, mode="on")
    spy = _SpyTool(tmp_workspace, "bash", "L2")
    registry.tools["bash"] = spy

    result = registry.run("bash", {"command": "python -c pass"})

    assert result.ok is False
    assert "requires approval" in result.content
    assert "bash" in result.content
    assert "L2" in result.content
    assert spy.calls == []


def test_on_mode_allows_l0_l1_and_web_search(tmp_workspace, memory, store):
    registry = _registry(tmp_workspace, memory, store, mode="on")
    web = _SpyTool(tmp_workspace, "web_search", "L2", requires_approval=False)
    registry.tools["web_search"] = web

    glob_result = registry.run("glob", {"pattern": "*"})
    write_result = registry.run("write_file", {"path": "a.txt", "content": "hello"})
    web_result = registry.run("web_search", {"query": "news"})

    assert glob_result.ok is True
    assert write_result.ok is True
    assert (tmp_workspace / "a.txt").read_text(encoding="utf-8") == "hello"
    assert web_result.ok is True
    assert web.calls == [{"query": "news"}]


def test_on_mode_scheduler_list_is_l0_create_is_l2(tmp_workspace, memory, store):
    registry = _registry(tmp_workspace, memory, store, mode="on")
    context = RunContext.create(
        session_id="cli:local",
        workspace=tmp_workspace,
        channel_type="cli",
        user_id="local",
    )

    listed = registry.run("scheduler", {"action": "list"}, context)
    created = registry.run(
        "scheduler",
        {
            "action": "create",
            "message": "hi",
            "schedule_type": "once",
            "schedule_value": "+10m",
        },
        context,
    )

    assert listed.ok is True
    assert json.loads(listed.content)["count"] == 0
    assert created.ok is False
    assert "requires approval" in created.content
    assert store.list() == []


@pytest.mark.parametrize("action", ["delete", "enable", "disable"])
def test_on_mode_scheduler_mutating_actions_are_l2(tmp_workspace, memory, store, action):
    registry = _registry(tmp_workspace, memory, store, mode="on")
    result = registry.run("scheduler", {"action": action, "task_id": "abcd1234"})
    assert result.ok is False
    assert "requires approval" in result.content


def test_on_mode_unknown_risk_is_denied_without_running(tmp_workspace, memory, store):
    registry = _registry(tmp_workspace, memory, store, mode="on")
    spy = _SpyTool(tmp_workspace, "weird", "LX")
    registry.tools["weird"] = spy

    result = registry.run("weird", {})

    assert result.ok is False
    assert "denied by policy" in result.content
    assert spy.calls == []


def test_off_mode_does_not_emit_policy_span(tmp_workspace, memory, store):
    registry = _registry(tmp_workspace, memory, store, mode="off")
    context = RunContext.create(session_id="cli:local", workspace=tmp_workspace)

    result = registry.run("write_file", {"path": "span.txt", "content": "x"}, context)

    assert result.ok is True
    assert [span.span_kind for span in context.trace.spans] == ["tool.execute"]


def test_on_mode_records_policy_span_under_execute(tmp_workspace, memory, store):
    registry = _registry(tmp_workspace, memory, store, mode="on")
    registry.tools["bash"] = _SpyTool(tmp_workspace, "bash", "L2")
    context = RunContext.create(session_id="cli:local", workspace=tmp_workspace)

    result = registry.run("bash", {"command": "python -c pass"}, context)

    assert result.ok is False
    kinds = [span.span_kind for span in context.trace.spans]
    assert kinds == ["tool.execute", "tool.policy"]
    execute_span, policy_span = context.trace.spans
    assert policy_span.parent_span_id == execute_span.span_id
    assert policy_span.attributes["decision"] == "need_approval"
    assert policy_span.attributes["risk_level"] == "L2"
    assert execute_span.attributes["error_type"] == "approval_required"
    assert execute_span.status == "error"


def test_on_mode_without_trace_still_denies_l2(tmp_workspace, memory, store):
    registry = _registry(tmp_workspace, memory, store, mode="on")
    spy = _SpyTool(tmp_workspace, "bash", "L2")
    registry.tools["bash"] = spy

    result = registry.run("bash", {"command": "python -c pass"})

    assert result.ok is False
    assert spy.calls == []


def test_invalid_json_is_rejected_before_policy(tmp_workspace, memory, store):
    policy = _CapturePolicy("on")
    registry = _registry(tmp_workspace, memory, store, policy=policy)
    context = RunContext.create(session_id="cli:local", workspace=tmp_workspace)

    result = registry.run("bash", "{bad-json", context)

    assert result.ok is False
    assert "Invalid JSON" in result.content
    assert policy.requests == []
    assert [span.span_kind for span in context.trace.spans] == ["tool.execute"]
    assert context.trace.spans[0].attributes["error_type"] == "invalid_json"


def test_hidden_scheduler_is_rejected_before_policy(tmp_workspace, memory, store):
    policy = _CapturePolicy("on")
    registry = _registry(tmp_workspace, memory, store, policy=policy)
    context = RunContext.create(
        session_id="scheduler:cli:local:abcd1234",
        workspace=tmp_workspace,
        is_scheduled_task=True,
    )

    result = registry.run("scheduler", {"action": "list"}, context)

    assert result.ok is False
    assert "not available" in result.content
    assert policy.requests == []


def test_scheduled_l2_is_denied_not_executed(tmp_workspace, memory, store):
    registry = _registry(tmp_workspace, memory, store, mode="on")
    spy = _SpyTool(tmp_workspace, "bash", "L2")
    registry.tools["bash"] = spy
    context = RunContext.create(
        session_id="scheduler:cli:local:abcd1234",
        workspace=tmp_workspace,
        is_scheduled_task=True,
        task_id="abcd1234",
    )

    result = registry.run("bash", {"command": "python -c pass"}, context)

    assert result.ok is False
    assert spy.calls == []


def test_registry_builds_request_from_context(tmp_workspace, memory, store):
    policy = _CapturePolicy("off")
    registry = _registry(tmp_workspace, memory, store, policy=policy)
    context = RunContext.create(
        session_id="cli:alice",
        workspace=tmp_workspace,
        channel_type="cli",
        user_id="alice",
        is_scheduled_task=True,
        task_id="deadbeef",
    )

    result = registry.run("write_file", {"path": "ctx.txt", "content": "x"}, context)

    assert result.ok is True
    request = policy.requests[0]
    assert request.name == "write_file"
    assert request.risk_level == "L1"
    assert request.session_id == "cli:alice"
    assert request.channel_type == "cli"
    assert request.user_id == "alice"
    assert request.is_scheduled_task is True
    assert request.task_id == "deadbeef"
    assert request.arguments["path"] == "ctx.txt"


def test_agent_passes_approval_mode_from_settings(settings_factory, tmp_path):
    (tmp_path / "prompts").mkdir(parents=True, exist_ok=True)
    (tmp_path / "prompts" / "identity.md").write_text("You are test agent.", encoding="utf-8")
    (tmp_path / "skills").mkdir(parents=True, exist_ok=True)
    settings = settings_factory(
        project_root=tmp_path,
        workspace=tmp_path / "workspace",
        tool_approval_mode="on",
    )
    agent = Agent(settings)
    try:
        assert agent.tools.policy.mode == "on"
    finally:
        agent.memory.close()
