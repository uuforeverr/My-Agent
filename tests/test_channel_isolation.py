from pathlib import Path

from agent.agent import Agent
from agent.run_context import RunContext
from tests.conftest import FakeLLM


def _prepare_project(root: Path):
    (root / "prompts").mkdir(parents=True, exist_ok=True)
    (root / "prompts" / "identity.md").write_text("You are test agent.", encoding="utf-8")
    (root / "skills").mkdir(parents=True, exist_ok=True)


def _ctx(session_id, workspace):
    return RunContext.create(session_id=session_id, workspace=workspace, entrypoint="test")


def test_sessions_do_not_share_short_term_history(settings_factory, tmp_path):
    _prepare_project(tmp_path)
    settings = settings_factory(project_root=tmp_path, workspace=tmp_path / "workspace")
    agent = Agent(settings)
    agent.llm = FakeLLM("ok")

    agent.run("我叫A", context=_ctx("cli:a", settings.workspace))
    agent.run("我是谁", context=_ctx("feishu:b", settings.workspace))

    # The second call belongs to session b; its model messages must not leak
    # session a's history. FakeLLM records every messages payload it received.
    b_messages = agent.llm.calls[-1]
    joined = " ".join(m.get("content") or "" for m in b_messages if isinstance(m.get("content"), str))
    assert "我叫A" not in joined
    assert "我是谁" in joined

    # Two distinct ContextManagers were created, one per session.
    assert set(agent._context_managers.keys()) == {"cli:a", "feishu:b"}
    agent.memory.close()


def test_same_session_history_accumulates(settings_factory, tmp_path):
    _prepare_project(tmp_path)
    settings = settings_factory(project_root=tmp_path, workspace=tmp_path / "workspace")
    agent = Agent(settings)
    agent.llm = FakeLLM("ok")

    agent.run("first", context=_ctx("cli:a", settings.workspace))
    agent.run("second", context=_ctx("cli:a", settings.workspace))

    # Same session reuses one ContextManager, so the second turn sees turn one.
    assert list(agent._context_managers.keys()) == ["cli:a"]
    second_messages = agent.llm.calls[-1]
    joined = " ".join(m.get("content") or "" for m in second_messages if isinstance(m.get("content"), str))
    assert "first" in joined
    assert "second" in joined
    agent.memory.close()
