import json
from pathlib import Path

from agent.agent import Agent
from memory.manager import MemoryManager
from tests.conftest import FakeLLM


def _prepare_project(root: Path) -> None:
    (root / "prompts").mkdir(parents=True, exist_ok=True)
    (root / "prompts" / "identity.md").write_text("You are test agent.", encoding="utf-8")
    (root / "skills").mkdir(parents=True, exist_ok=True)


def _write_turn(manager: MemoryManager, session_id: str, index: int, prefix="t", tool_blob=""):
    manager.record_message(session_id, "user", f"{prefix}-user-{index}")
    if tool_blob and index == 0:
        manager.record_message(
            session_id,
            "assistant",
            f"{prefix}-asst-{index}",
            metadata={"tool_calls": [{"id": "c1", "function": {"name": "bash"}}]},
        )
        manager.record_message(
            session_id,
            "tool",
            tool_blob,
            name="bash",
            metadata={"tool_call_id": "c1"},
        )
    else:
        manager.record_message(session_id, "assistant", f"{prefix}-asst-{index}")


def _watermark(manager: MemoryManager, session_id: str) -> int:
    path = manager.memory_dir / "episodic_watermark.json"
    if not path.exists():
        return 0
    data = json.loads(path.read_text(encoding="utf-8"))
    return int((data.get(session_id) or {}).get("flushed_user_turns") or 0)


def test_scan_extracts_only_turns_beyond_keep_recent(tmp_memory_dir: Path):
    llm = FakeLLM(["- extracted first turn"])
    manager = MemoryManager(tmp_memory_dir, llm=llm)
    session_id = "cli:local:20260706-153012-a3f9"
    for index in range(4):
        _write_turn(manager, session_id, index, tool_blob="TOOL_SHOULD_NOT_APPEAR")

    result = manager.scan_raw_for_episodic(keep_recent_turns=3, min_flush_turns=0)

    assert result["ok"] is True
    prompt = llm.calls[0][-1]["content"]
    assert "t-user-0" in prompt
    assert "t-asst-0" in prompt
    assert "t-user-1" not in prompt
    assert "t-user-3" not in prompt
    assert _watermark(manager, session_id) == 1
    daily = manager.read_today_episodic_memory()
    assert "extracted first turn" in daily
    manager.close()


def test_scan_ignores_tool_messages_in_prompt_to_curator(tmp_memory_dir: Path):
    tool_blob = "TOOL_SECRET_PAYLOAD_" + ("x" * 400)
    llm = FakeLLM(["- ignored tools"])
    manager = MemoryManager(tmp_memory_dir, llm=llm)
    session_id = "cli:local:20260706-153012-b4c0"
    for index in range(4):
        _write_turn(manager, session_id, index, tool_blob=tool_blob)

    manager.scan_raw_for_episodic(keep_recent_turns=3, min_flush_turns=0)

    dumped = json.dumps(llm.calls, ensure_ascii=False)
    assert tool_blob not in dumped
    assert "TOOL_SECRET_PAYLOAD_" not in dumped
    assert "t-user-0" in dumped
    manager.close()


def test_scan_advances_watermark_when_summary_is_none(tmp_memory_dir: Path):
    llm = FakeLLM("无")
    manager = MemoryManager(tmp_memory_dir, llm=llm)
    session_id = "cli:local:20260706-153012-c1d2"
    for index in range(4):
        _write_turn(manager, session_id, index)

    result = manager.scan_raw_for_episodic(keep_recent_turns=3, min_flush_turns=0)

    assert result["ok"] is True
    assert _watermark(manager, session_id) == 1
    assert "extracted" not in manager.read_today_episodic_memory()
    assert len(llm.calls) == 1
    manager.close()


def test_second_scan_is_noop_until_new_turns(tmp_memory_dir: Path):
    llm = FakeLLM(["- first extract", "- second extract"])
    manager = MemoryManager(tmp_memory_dir, llm=llm)
    session_id = "cli:local:20260706-153012-d3e4"
    for index in range(4):
        _write_turn(manager, session_id, index)

    manager.scan_raw_for_episodic(keep_recent_turns=3, min_flush_turns=0)
    assert len(llm.calls) == 1
    assert _watermark(manager, session_id) == 1

    manager.scan_raw_for_episodic(keep_recent_turns=3, min_flush_turns=0)
    assert len(llm.calls) == 1
    assert _watermark(manager, session_id) == 1

    _write_turn(manager, session_id, 4)
    manager.scan_raw_for_episodic(keep_recent_turns=3, min_flush_turns=0)
    assert len(llm.calls) == 2
    prompt = llm.calls[1][-1]["content"]
    assert "t-user-1" in prompt
    assert "t-user-0" not in prompt
    assert "t-user-2" not in prompt
    assert _watermark(manager, session_id) == 2
    manager.close()


def test_scan_skips_when_only_two_user_turns(tmp_memory_dir: Path):
    llm = FakeLLM(["- should not run"])
    manager = MemoryManager(tmp_memory_dir, llm=llm)
    session_id = "cli:local:20260706-153012-e5f6"
    for index in range(2):
        _write_turn(manager, session_id, index, tool_blob="lots-of-tool-output")
        manager.record_message(session_id, "tool", "more-tool", name="bash", metadata={"tool_call_id": "x"})
        manager.record_message(session_id, "tool", "even-more-tool", name="bash", metadata={"tool_call_id": "y"})

    manager.scan_raw_for_episodic(keep_recent_turns=3, min_flush_turns=0)
    assert llm.calls == []
    assert _watermark(manager, session_id) == 0
    manager.close()


def test_hydrate_restores_history_including_tool_from_raw(settings_factory, tmp_path):
    _prepare_project(tmp_path)
    settings = settings_factory(project_root=tmp_path, workspace=tmp_path / "workspace")
    writer = Agent(settings)
    session_a = "cli:local:20260706-153012-aaa1"
    session_b = "cli:local:20260706-153013-bbb2"
    writer.memory.record_message(session_a, "user", "read the file")
    writer.memory.record_message(
        session_a,
        "assistant",
        "",
        metadata={"tool_calls": [{"id": "call-1", "function": {"name": "read_file"}}]},
    )
    writer.memory.record_message(
        session_a,
        "tool",
        "file contents",
        name="read_file",
        metadata={"tool_call_id": "call-1"},
    )
    writer.memory.record_message(session_a, "assistant", "done reading")
    writer.memory.record_message(session_b, "user", "other conversation")
    writer.memory.close()

    reader = Agent(settings)
    history = reader._get_context_manager(session_a).history
    assert history[0] == {"role": "user", "content": "read the file"}
    assert history[1]["role"] == "assistant"
    assert history[1]["tool_calls"][0]["id"] == "call-1"
    assert history[2] == {
        "role": "tool",
        "content": "file contents",
        "name": "read_file",
        "tool_call_id": "call-1",
    }
    assert history[3] == {"role": "assistant", "content": "done reading"}

    other = reader._get_context_manager(session_b).history
    assert [m.get("content") for m in other] == ["other conversation"]
    assert "read the file" not in [m.get("content") for m in other]
    reader.memory.close()


def test_episodic_search_sees_events_from_two_conversations(tmp_memory_dir: Path):
    llm = FakeLLM(["- 会话甲独立事件标记", "- 会话乙独立事件标记"])
    manager = MemoryManager(tmp_memory_dir, llm=llm)
    sid_a = "cli:local:20260706-153012-aaa1"
    sid_b = "cli:local:20260706-153013-bbb2"
    for index in range(4):
        _write_turn(manager, sid_a, index, prefix="alpha")
        _write_turn(manager, sid_b, index, prefix="beta")

    manager.scan_raw_for_episodic(keep_recent_turns=3, min_flush_turns=0)

    prompt, count = manager.build_prompt("独立事件标记", limit=10)
    assert "会话甲独立事件标记" in prompt
    assert "会话乙独立事件标记" in prompt
    assert count >= 2
    manager.close()


def test_scan_waits_until_pending_exceeds_min_plus_keep(tmp_memory_dir: Path):
    llm = FakeLLM(["- batched extract"])
    manager = MemoryManager(tmp_memory_dir, llm=llm)
    session_id = "cli:local:20260706-153012-batch"
    for index in range(23):
        _write_turn(manager, session_id, index)

    skipped = manager.scan_raw_for_episodic(keep_recent_turns=3, min_flush_turns=20)
    assert llm.calls == []
    assert _watermark(manager, session_id) == 0
    assert skipped["sessions"][0]["reason"] == "below_flush_threshold"
    assert skipped["sessions"][0]["threshold"] == 23

    _write_turn(manager, session_id, 23)
    flushed = manager.scan_raw_for_episodic(keep_recent_turns=3, min_flush_turns=20)
    assert len(llm.calls) == 1
    assert _watermark(manager, session_id) == 21
    prompt = llm.calls[0][-1]["content"]
    assert "t-user-0" in prompt
    assert "t-user-20" in prompt
    assert "t-user-21" not in prompt
    assert flushed["sessions"][0]["extracted_turns"] == 21
    manager.close()
