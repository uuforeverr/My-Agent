from pathlib import Path

from agent.agent import Agent
from memory.manager import MemoryManager
from tests.conftest import FakeLLM


def _prepare_project(root: Path) -> None:
    (root / "prompts").mkdir(parents=True, exist_ok=True)
    (root / "prompts" / "identity.md").write_text("You are test agent.", encoding="utf-8")
    (root / "skills").mkdir(parents=True, exist_ok=True)


def _write_turn(manager: MemoryManager, session_id: str, index: int):
    receipt = manager.record_message(session_id, "user", f"user-{index}")
    manager.record_message(
        session_id,
        "assistant",
        f"assistant-{index}",
        turn_seq=receipt.turn_seq,
    )
    manager.raw_store.flush()
    return manager.raw_store.read_recent_turns(session_id, 1)[0]


def test_pending_batch_extracts_structured_episode_and_advances_cursor(tmp_memory_dir: Path):
    llm = FakeLLM(
        '{"body":"确定采用异步 Raw 写入。","stable_facts":["Raw 使用 SQLite"],'
        '"lessons":["主链路不等待落盘"]}'
    )
    # A single object is invalid by contract; fallback should not create an episode.
    manager = MemoryManager(tmp_memory_dir, llm=llm, episodic_batch_turns=2)
    turn1 = _write_turn(manager, "s", 1)
    turn2 = _write_turn(manager, "s", 2)
    manager.enqueue_pending_turns("s", [turn1, turn2])

    result = manager._process_pending_session("s")

    assert result["processed_turns"] == 2
    assert result["episodic_items"] == 0
    assert manager._episodic_cursor("s") == 2
    assert manager.pending_count("s") == 0
    manager.close()


def test_pending_episode_keeps_body_facts_and_lessons(tmp_memory_dir: Path):
    llm = FakeLLM(
        '[{"body":"确定采用异步 Raw 写入。","stable_facts":["Raw 使用 SQLite"],'
        '"lessons":["主链路不等待落盘"]}]'
    )
    manager = MemoryManager(tmp_memory_dir, llm=llm, episodic_batch_turns=1)
    manager.enqueue_pending_turns("s", [_write_turn(manager, "s", 1)])

    result = manager._process_pending_session("s")

    assert result["episodic_items"] == 1
    episodic = manager.read_today_episodic_memory()
    assert "确定采用异步 Raw 写入" in episodic
    assert "Raw 使用 SQLite" in episodic
    assert "主链路不等待落盘" in episodic
    assert manager.search("异步 Raw", limit=5)
    manager.close()


def test_failed_extraction_keeps_pending_turns(tmp_memory_dir: Path):
    class BoomLLM:
        def chat(self, messages, tools=None):
            raise RuntimeError("boom")

    manager = MemoryManager(tmp_memory_dir, llm=BoomLLM(), episodic_batch_turns=1)
    manager.enqueue_pending_turns("s", [_write_turn(manager, "s", 1)])

    result = manager._process_pending_session("s")

    assert result["ok"] is False
    assert manager.pending_count("s") == 1
    assert manager._episodic_cursor("s") == 0
    manager.close()


def test_recovery_rebuilds_only_cursor_to_before_recent_window(tmp_memory_dir: Path):
    manager = MemoryManager(tmp_memory_dir, llm=FakeLLM("[]"), episodic_batch_turns=20)
    for index in range(5):
        _write_turn(manager, "s", index)
    with manager.state_database.write() as conn:
        conn.execute(
            "INSERT INTO episodic_cursors(session_id, last_turn_seq, updated_at) VALUES('s', 1, 'now')"
        )

    recovered = manager.recover_pending("s", session_window_turns=2)

    assert recovered == 2
    assert manager.pending_count("s") == 2
    manager.close()


def test_agent_hydrates_only_configured_recent_complete_turns(settings_factory, tmp_path):
    _prepare_project(tmp_path)
    settings = settings_factory(
        project_root=tmp_path,
        workspace=tmp_path / "workspace",
        session_window_turns=2,
    )
    writer = Agent(settings)
    for index in range(4):
        _write_turn(writer.memory, "cli:local", index)
    writer.memory.close()

    reader = Agent(settings)
    history = reader._get_context_manager("cli:local").history

    assert [item["content"] for item in history if item["role"] == "user"] == ["user-2", "user-3"]
    assert reader.memory.pending_count("cli:local") == 2
    reader.memory.close()
