import os

import pytest

from agent.agent import Agent
from agent.run_context import RunContext
from config import load_settings


@pytest.mark.live
def test_live_deepseek_agent_reads_file_once(tmp_path, monkeypatch):
    if os.environ.get("RUN_LIVE_AGENT_E2E") != "1":
        pytest.skip("set RUN_LIVE_AGENT_E2E=1 to run real Agent + DeepSeek E2E")

    base = load_settings()
    project_root = tmp_path / "project"
    workspace = project_root / "workspace"
    workspace.mkdir(parents=True)
    (project_root / "prompts").mkdir()
    (project_root / "prompts" / "identity.md").write_text(
        "你是测试助手。用户要求读取文件时必须用工具。最终只回复一句中文总结。",
        encoding="utf-8",
    )
    (project_root / "skills").mkdir()
    (workspace / "a.txt").write_text(
        "my-agent memory uses semantic memory and episodic memory.",
        encoding="utf-8",
    )

    settings = type(base)(
        project_root=project_root,
        openai_api_key=base.openai_api_key,
        openai_api_base=base.openai_api_base,
        model=base.model,
        workspace=workspace,
        max_steps=4,
        max_history_messages=20,
        memory_search_results=2,
        memory_flush_messages=4,
        bash_timeout=5,
    )
    agent = Agent(settings)
    try:
        answer, trace = agent.run(
            "请读取 a.txt，并用一句中文总结文件内容。",
            context=RunContext.create("live", workspace, entrypoint="test"),
        )
    finally:
        agent.memory.close(wait=True)

    assert answer.strip()
    raw_records = agent.memory.raw_store.read_day("live")
    assert any(record["role"] == "tool" for record in raw_records)
    assert any(event["name"] == "tool_call" for event in trace.events)
