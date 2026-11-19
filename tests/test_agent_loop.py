from pathlib import Path
import json

from agent.agent import Agent
from tests.conftest import FakeLLM


class _FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def _prepare_project(root: Path):
    (root / "prompts").mkdir(parents=True, exist_ok=True)
    (root / "prompts" / "identity.md").write_text("You are test agent.", encoding="utf-8")
    (root / "skills").mkdir(parents=True, exist_ok=True)


def test_agent_run_without_tool_records_raw_and_final(settings_factory, tmp_path, monkeypatch, otel_exporter):
    _prepare_project(tmp_path)
    settings = settings_factory(project_root=tmp_path, workspace=tmp_path / "workspace")
    agent = Agent(settings)
    agent.llm = FakeLLM({"role": "assistant", "content": "final answer"})

    answer, result = agent.run("hello")

    assert answer == "final answer"
    raw_records = agent.memory.raw_store.read_day("default")
    assert [r["role"] for r in raw_records] == ["user", "assistant"]
    assert raw_records[0]["content"] == "hello"
    assert result.outcome == "success"
    spans = otel_exporter.get_finished_spans()
    names = [span.name for span in spans]
    assert "invoke_agent my-agent" in names
    assert "assemble_context" in names
    assert "retrieval memory" in names
    assert "chat fake-model" in names
    agent_span = next(span for span in spans if span.name == "invoke_agent my-agent")
    step_span = next(span for span in spans if span.name == "agent.step 1")
    context_span = next(span for span in spans if span.name == "assemble_context")
    llm_span = next(span for span in spans if span.name == "chat fake-model")
    assert step_span.parent.span_id == agent_span.context.span_id
    assert context_span.parent.span_id == step_span.context.span_id
    assert llm_span.parent.span_id == step_span.context.span_id
    tool_names = list(llm_span.attributes["my_agent.tool.names"])
    assert "read_file" in tool_names
    assert "web_search" not in tool_names
    assert agent_span.attributes["my_agent.stopped_reason"] == "final_answer"
    assert any(event.name == "final_answer" for event in agent_span.events)
    agent.memory.close()


def test_agent_run_with_tool_call(settings_factory, tmp_path, otel_exporter):
    _prepare_project(tmp_path)
    (tmp_path / "workspace" / "a.txt").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "workspace" / "a.txt").write_text("hello file", encoding="utf-8")
    settings = settings_factory(project_root=tmp_path, workspace=tmp_path / "workspace")
    agent = Agent(settings)
    agent.llm = FakeLLM([
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "function": {"name": "read_file", "arguments": '{"path":"a.txt"}'},
                }
            ],
        },
        {"role": "assistant", "content": "read complete"},
    ])

    answer, result = agent.run("read a.txt")

    assert answer == "read complete"
    assert result.outcome == "success"
    spans = otel_exporter.get_finished_spans()
    tool_span = next(span for span in spans if span.name == "execute_tool read_file")
    steps = {span.name: span for span in spans if span.name.startswith("agent.step ")}
    chats = [span for span in spans if span.name == "chat fake-model"]
    contexts = [span for span in spans if span.name == "assemble_context"]
    assert tool_span.attributes["gen_ai.tool.name"] == "read_file"
    assert tool_span.attributes["my_agent.tool.ok"] is True
    assert tool_span.parent.span_id == steps["agent.step 1"].context.span_id
    assert chats[0].parent.span_id == steps["agent.step 1"].context.span_id
    assert chats[1].parent.span_id == steps["agent.step 2"].context.span_id
    assert contexts[0].parent.span_id == steps["agent.step 1"].context.span_id
    assert contexts[1].parent.span_id == steps["agent.step 2"].context.span_id
    raw_records = agent.memory.raw_store.read_day("default")
    assert [r["role"] for r in raw_records] == ["user", "assistant", "tool", "assistant"]
    assert "hello file" in raw_records[2]["content"]
    agent.memory.close()


def test_agent_scheduler_call_exposes_task_correlation_on_root_span(settings_factory, tmp_path, otel_exporter):
    _prepare_project(tmp_path)
    settings = settings_factory(
        project_root=tmp_path,
        workspace=tmp_path / "workspace",
        tool_approval_mode="never",
    )
    agent = Agent(settings)
    agent.llm = FakeLLM([
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-scheduler",
                    "function": {
                        "name": "scheduler",
                        "arguments": json.dumps({
                            "action": "create",
                            "name": "trace correlation",
                            "message": "done",
                            "schedule_type": "once",
                            "schedule_value": "+10m",
                            "timing_semantics": "notify_at",
                        }),
                    },
                }
            ],
        },
        {"role": "assistant", "content": "created"},
    ])
    from agent.run_context import RunContext

    context = RunContext.create(
        session_id="cli:local",
        workspace=settings.workspace,
        channel_type="cli",
        user_id="local",
    )
    answer, result = agent.run("remind me", context=context)

    assert answer == "created"
    assert result.outcome == "success"
    spans = otel_exporter.get_finished_spans()
    root = next(span for span in spans if span.name == "invoke_agent my-agent")
    tool = next(span for span in spans if span.name == "execute_tool scheduler")
    task_id = tool.attributes["my_agent.task.id"]
    assert root.attributes["my_agent.task.id"] == task_id
    assert list(root.attributes["my_agent.task.ids"]) == [task_id]
    assert tool.attributes["my_agent.operation"] == "scheduler.create"
    agent.memory.close()


def test_agent_run_emits_progress_for_tool_call_content(settings_factory, tmp_path):
    _prepare_project(tmp_path)
    (tmp_path / "workspace" / "a.txt").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "workspace" / "a.txt").write_text("hello file", encoding="utf-8")
    settings = settings_factory(project_root=tmp_path, workspace=tmp_path / "workspace")
    agent = Agent(settings)
    agent.llm = FakeLLM([
        {
            "role": "assistant",
            "content": "让我翻翻文件～",
            "tool_calls": [
                {
                    "id": "call-1",
                    "function": {"name": "read_file", "arguments": '{"path":"a.txt"}'},
                }
            ],
        },
        {"role": "assistant", "content": "read complete"},
    ])
    events = []

    answer, result = agent.run("read a.txt", on_event=events.append)

    assert answer == "read complete"
    assert events == [{
        "type": "assistant_progress",
        "content": "让我翻翻文件～",
        "step": 1,
        "tool_calls": [
            {
                "id": "call-1",
                "function": {"name": "read_file", "arguments": '{"path":"a.txt"}'},
            }
        ],
    }]
    assert result.outcome == "success"
    agent.memory.close()


def test_agent_run_with_web_search_tool_call(settings_factory, tmp_path, monkeypatch):
    _prepare_project(tmp_path)
    settings = settings_factory(
        project_root=tmp_path,
        workspace=tmp_path / "workspace",
        qianfan_api_key="qf-key",
        qianfan_api_base="https://qianfan.example/v2",
    )
    agent = Agent(settings)
    agent.llm = FakeLLM([
        {
            "role": "assistant",
            "content": "我查一下最新信息～",
            "tool_calls": [
                {
                    "id": "call-search",
                    "function": {
                        "name": "web_search",
                        "arguments": '{"query":"百度千帆 AI 搜索","count":1}',
                    },
                }
            ],
        },
        {"role": "assistant", "content": "查到了"},
    ])
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout: _FakeResponse({
            "references": [
                {
                    "title": "千帆 AI 搜索",
                    "url": "https://example.com/qianfan",
                    "snippet": "AI search result",
                }
            ]
        }),
    )

    events = []
    answer, result = agent.run("查一下千帆", on_event=events.append)

    assert answer == "查到了"
    assert events[0]["content"] == "我查一下最新信息～"
    assert result.outcome == "success"
    raw_records = agent.memory.raw_store.read_day("default")
    assert raw_records[2]["role"] == "tool"
    assert "千帆 AI 搜索" in raw_records[2]["content"]
    agent.memory.close()


def test_agent_max_steps_fallback(settings_factory, tmp_path):
    _prepare_project(tmp_path)
    settings = settings_factory(project_root=tmp_path, workspace=tmp_path / "workspace", max_steps=1)
    agent = Agent(settings)
    agent.llm = FakeLLM({
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": "call-1", "function": {"name": "glob", "arguments": '{"pattern":"*.missing"}'}}
        ],
    })

    answer, result = agent.run("loop")

    assert "Reached max_steps=1" in answer
    raw_records = agent.memory.raw_store.read_day("default")
    assert raw_records[-1]["metadata"]["reason"] == "max_steps_reached"
    assert result.outcome == "max_steps"
    agent.memory.close()


def test_summarize_session_compacts_without_agent_loop(settings_factory, tmp_path):
    _prepare_project(tmp_path)
    settings = settings_factory(
        project_root=tmp_path,
        workspace=tmp_path / "workspace",
        keep_recent_turns=1,
        model_context_window=100000,
    )
    agent = Agent(settings)
    agent.llm = FakeLLM("unused")
    manager = agent._get_context_manager("cli:local")
    manager.append_user_message("u0")
    manager.append_assistant_message({"role": "assistant", "content": "a0"})
    manager.append_user_message("u1")
    manager.append_assistant_message({"role": "assistant", "content": "a1"})

    assert agent.summarize_session("cli:local") == "已压缩"
    assert [item["content"] for item in manager.history] == ["u1", "a1"]
    assert agent.summarize_session("fresh") == "无需压缩"
    agent.memory.close()
