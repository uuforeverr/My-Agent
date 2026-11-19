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
    def __init__(self, mode="never"):
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


def _registry(tmp_workspace, memory, store, mode="never", policy=None):
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
        "risk_level": "R2",
    }
    payload.update(overrides)
    return ToolRequest(**payload)


def test_normalize_approval_mode_supports_new_modes_and_legacy_aliases():
    assert normalize_approval_mode("always") == "always"
    assert normalize_approval_mode("NEVER") == "never"
    assert normalize_approval_mode("risk_based") == "risk_based"
    assert normalize_approval_mode("on") == "always"
    assert normalize_approval_mode(" off ") == "never"
    assert normalize_approval_mode("true") == "invalid"
    assert normalize_approval_mode("") == "invalid"
    assert ToolPolicy("ALWAYS").mode == "always"


def test_policy_never_allows_r0_to_r2_but_denies_unknown():
    policy = ToolPolicy("never")
    for risk in ("R0", "R1", "R2"):
        decision = policy.decide(_request(risk_level=risk))
        assert decision.action == "allow"
        assert decision.reason == "approval_mode_never"
    assert policy.decide(_request(risk_level="R3")).reason == "unknown_risk_level"
    assert policy.decide(_request(risk_level="R9")).reason == "unknown_risk_level"


def test_policy_always_asks_for_every_permitted_model_tool():
    policy = ToolPolicy("always")
    for name, risk in (("glob", "R0"), ("write_file", "R1"), ("bash", "R2")):
        decision = policy.decide(_request(name=name, risk_level=risk))
        assert decision.action == "need_approval"
        assert decision.reason == "approval_mode_always"
    web = policy.decide(_request(name="web_search", risk_level="R1", requires_approval=False))
    assert web.action == "need_approval"
    assert policy.decide(_request(name="future", risk_level="R3")).action == "deny"
    unknown = policy.decide(_request(risk_level="LX"))
    assert unknown.action == "deny"
    assert unknown.reason == "unknown_risk_level"


def test_policy_risk_based_allows_r0_r1_and_requires_approval_for_r2():
    policy = ToolPolicy("risk_based")
    for risk in ("R0", "R1"):
        decision = policy.decide(_request(risk_level=risk))
        assert decision.action == "allow"
        assert decision.reason == "risk_based_low_risk"
    r2 = policy.decide(_request(risk_level="R2"))
    assert r2.action == "need_approval"
    assert r2.reason == "risk_based_r2"
    assert policy.decide(_request(risk_level="R3")).action == "deny"


def test_static_risk_table():
    assert GlobTool.risk_level == "R0"
    assert ReadFileTool.risk_level == "R0"
    assert SearchMemoryTool.risk_level == "R0"
    assert ReadMemoryTool.risk_level == "R0"
    assert WriteFileTool.risk_level == "R1"
    assert EditFileTool.risk_level == "R1"
    assert RememberMemoryTool.risk_level == "R1"
    assert WriteFileTool.idempotent is False
    assert BashTool.risk_level == "R2"
    assert BashTool.idempotent is False
    assert WebSearchTool.risk_level == "R1"
    assert WebSearchTool.requires_approval is False
    assert SchedulerTool.risk_level == "R2"


def test_scheduler_risk_for_by_action(tmp_workspace, tmp_path):
    tool = SchedulerTool(tmp_workspace, TaskStore(tmp_path / "tasks.json"))
    assert tool.risk_for({"action": "list"}) == "R0"
    assert tool.risk_for({"action": "get"}) == "R0"
    assert tool.risk_for({"action": "create"}) == "R2"
    assert tool.risk_for({"action": "delete"}) == "R2"
    assert tool.risk_for({"action": "enable"}) == "R2"
    assert tool.risk_for({"action": "disable"}) == "R2"
    assert tool.risk_for({"action": "unknown"}) == "R2"
    assert tool.risk_for({}) == "R2"


def test_off_mode_still_executes_l2(tmp_workspace, memory, store):
    registry = _registry(tmp_workspace, memory, store, mode="never")
    spy = _SpyTool(tmp_workspace, "bash", "L2")
    registry.tools["bash"] = spy

    result = registry.run("bash", {"command": "python -c pass"})

    assert result.ok is True
    assert spy.calls == [{"command": "python -c pass"}]


def test_on_mode_does_not_execute_l2_without_gate(tmp_workspace, memory, store):
    registry = _registry(tmp_workspace, memory, store, mode="always")
    spy = _SpyTool(tmp_workspace, "bash", "L2")
    registry.tools["bash"] = spy

    result = registry.run("bash", {"command": "python -c pass"})

    assert result.ok is False
    assert "requires approval" in result.content
    assert "bash" in result.content
    assert "R2" in result.content
    assert spy.calls == []


def test_never_mode_allows_r0_r1_and_web_search(tmp_workspace, memory, store):
    registry = _registry(tmp_workspace, memory, store, mode="never")
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


def test_never_mode_scheduler_list_and_create_execute(tmp_workspace, memory, store):
    registry = _registry(tmp_workspace, memory, store, mode="never")
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
    assert created.ok is True
    assert len(store.list()) == 1


def test_risk_based_registry_runs_r0_r1_but_stops_r2_without_gate(
    tmp_workspace, memory, store
):
    registry = _registry(tmp_workspace, memory, store, mode="risk_based")
    spy = _SpyTool(tmp_workspace, "bash", "R2")
    registry.tools["bash"] = spy

    assert registry.run("glob", {"pattern": "*"}).ok is True
    assert registry.run("write_file", {"path": "r1.txt", "content": "ok"}).ok is True
    denied = registry.run("bash", {"command": "python -c pass"})

    assert denied.ok is False
    assert denied.error_code == "approval_required"
    assert spy.calls == []


def test_risk_based_read_file_env_is_r0_but_bash_env_access_remains_denied(
    tmp_workspace, memory, store
):
    (tmp_workspace / ".env").write_text("EXAMPLE=value\n", encoding="utf-8")
    registry = _registry(tmp_workspace, memory, store, mode="risk_based")

    read_result = registry.run("read_file", {"path": ".env"})
    bash_result = registry.run("bash", {"command": "cat .env"})

    assert read_result.ok is True
    assert "EXAMPLE=value" in read_result.content
    assert bash_result.ok is False
    assert "credential" in bash_result.content.lower()


@pytest.mark.parametrize("action", ["delete", "enable", "disable"])
def test_on_mode_scheduler_mutating_actions_are_l2(tmp_workspace, memory, store, action):
    registry = _registry(tmp_workspace, memory, store, mode="always")
    result = registry.run("scheduler", {"action": action, "task_id": "abcd1234"})
    assert result.ok is False
    assert "requires approval" in result.content


def test_on_mode_unknown_risk_is_denied_without_running(tmp_workspace, memory, store):
    registry = _registry(tmp_workspace, memory, store, mode="always")
    spy = _SpyTool(tmp_workspace, "weird", "LX")
    registry.tools["weird"] = spy

    result = registry.run("weird", {})

    assert result.ok is False
    assert "denied by policy" in result.content
    assert spy.calls == []


def test_never_mode_still_emits_policy_span(tmp_workspace, memory, store, otel_exporter):
    registry = _registry(tmp_workspace, memory, store, mode="never")
    context = RunContext.create(session_id="cli:local", workspace=tmp_workspace)

    result = registry.run("write_file", {"path": "span.txt", "content": "x"}, context)

    assert result.ok is True
    assert {span.name for span in otel_exporter.get_finished_spans()} == {"execute_tool write_file", "tool.policy"}


def test_on_mode_records_policy_span_under_execute(tmp_workspace, memory, store, otel_exporter):
    registry = _registry(tmp_workspace, memory, store, mode="always")
    registry.tools["bash"] = _SpyTool(tmp_workspace, "bash", "L2")
    context = RunContext.create(session_id="cli:local", workspace=tmp_workspace)

    result = registry.run("bash", {"command": "python -c pass"}, context)

    assert result.ok is False
    spans = otel_exporter.get_finished_spans()
    execute_span = next(span for span in spans if span.name == "execute_tool bash")
    policy_span = next(span for span in spans if span.name == "tool.policy")
    assert policy_span.parent.span_id == execute_span.context.span_id
    assert policy_span.attributes["my_agent.policy.decision"] == "need_approval"
    assert policy_span.attributes["my_agent.policy.mode"] == "always"
    assert policy_span.attributes["my_agent.tool.risk_level"] == "R2"
    assert execute_span.attributes["error.type"] == "approval_required"
    assert execute_span.status.status_code.name == "ERROR"


def test_on_mode_without_trace_still_denies_l2(tmp_workspace, memory, store):
    registry = _registry(tmp_workspace, memory, store, mode="always")
    spy = _SpyTool(tmp_workspace, "bash", "L2")
    registry.tools["bash"] = spy

    result = registry.run("bash", {"command": "python -c pass"})

    assert result.ok is False
    assert spy.calls == []


def test_invalid_json_is_rejected_before_policy(tmp_workspace, memory, store, otel_exporter):
    policy = _CapturePolicy("always")
    registry = _registry(tmp_workspace, memory, store, policy=policy)
    context = RunContext.create(session_id="cli:local", workspace=tmp_workspace)

    result = registry.run("bash", "{bad-json", context)

    assert result.ok is False
    assert "Invalid JSON" in result.content
    assert policy.requests == []
    spans = otel_exporter.get_finished_spans()
    assert [span.name for span in spans] == ["execute_tool bash"]
    assert spans[0].attributes["error.type"] == "invalid_json"


def test_hidden_scheduler_is_rejected_before_policy(tmp_workspace, memory, store):
    policy = _CapturePolicy("always")
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
    registry = _registry(tmp_workspace, memory, store, mode="always")
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
    policy = _CapturePolicy("never")
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
    assert request.risk_level == "R1"
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
        tool_approval_mode="always",
    )
    agent = Agent(settings)
    try:
        assert agent.tools.policy.mode == "always"
    finally:
        agent.memory.close()
