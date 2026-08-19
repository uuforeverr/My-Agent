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


def test_agent_run_without_tool_records_raw_and_final(settings_factory, tmp_path, monkeypatch):
    _prepare_project(tmp_path)
    settings = settings_factory(project_root=tmp_path, workspace=tmp_path / "workspace")
    agent = Agent(settings)
    agent.llm = FakeLLM({"role": "assistant", "content": "final answer"})

    answer, trace = agent.run("hello")

    assert answer == "final answer"
    raw_records = agent.memory.raw_store.read_day("default")
    assert [r["role"] for r in raw_records] == ["user", "assistant"]
    assert raw_records[0]["content"] == "hello"
    assert any(e["name"] == "final_answer" for e in trace.events)
    span_kinds = [span.span_kind for span in trace.spans]
    assert "agent.invoke" in span_kinds
    assert "context.build" in span_kinds
    assert "memory.inject" in span_kinds
    assert "skill.catalog.load" in span_kinds
    assert "skill.catalog.inject" in span_kinds
    assert "skill.activate" not in span_kinds
    assert "llm.chat" in span_kinds
    agent_span = next(span for span in trace.spans if span.span_kind == "agent.invoke")
    context_span = next(span for span in trace.spans if span.span_kind == "context.build")
    llm_span = next(span for span in trace.spans if span.span_kind == "llm.chat")
    assert context_span.parent_span_id == agent_span.span_id
    assert llm_span.parent_span_id == agent_span.span_id
    assert agent_span.attributes["stopped_reason"] == "final_answer"
    agent.memory.close()


def test_agent_run_with_tool_call(settings_factory, tmp_path):
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

    answer, trace = agent.run("read a.txt")

    assert answer == "read complete"
    assert any(e["name"] == "tool_call" and e["tool_name"] == "read_file" for e in trace.events)
    tool_span = next(span for span in trace.spans if span.span_kind == "tool.execute")
    assert tool_span.attributes["tool_name"] == "read_file"
    assert tool_span.attributes["ok"] is True
    assert tool_span.status == "success"
    raw_records = agent.memory.raw_store.read_day("default")
    assert [r["role"] for r in raw_records] == ["user", "assistant", "tool", "assistant"]
    assert "hello file" in raw_records[2]["content"]
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

    answer, trace = agent.run("read a.txt", on_event=events.append)

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
    assert any(e["name"] == "tool_call" for e in trace.events)
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
    answer, trace = agent.run("查一下千帆", on_event=events.append)

    assert answer == "查到了"
    assert events[0]["content"] == "我查一下最新信息～"
    assert any(e["name"] == "tool_call" and e["tool_name"] == "web_search" for e in trace.events)
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

    answer, trace = agent.run("loop")

    assert "Reached max_steps=1" in answer
    raw_records = agent.memory.raw_store.read_day("default")
    assert raw_records[-1]["metadata"]["reason"] == "max_steps_reached"
    assert any(e["name"] == "max_steps_reached" for e in trace.events)
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
    agent.llm = FakeLLM("agent-summary")
    manager = agent._get_context_manager("cli:local")
    manager.append_user_message("u0")
    manager.append_assistant_message({"role": "assistant", "content": "a0"})
    manager.append_user_message("u1")
    manager.append_assistant_message({"role": "assistant", "content": "a1"})

    assert agent.summarize_session("cli:local") == "已压缩"
    assert "agent-summary" in manager.history[0]["content"]
    assert agent.summarize_session("fresh") == "无需压缩"
    agent.memory.close()
