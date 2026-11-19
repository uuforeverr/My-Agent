from agent.compactor import split_recent_turns
from agent.context_manager import ContextManager
from agent.run_context import RunContext
from agent.token_counter import TokenCounter


class FakeMemory:
    def __init__(self):
        self.pending = []
        self.build_calls = 0

    def build_prompt(self, query, limit=5):
        self.build_calls += 1
        return f"MEMORY for {query}", 1

    def enqueue_pending_messages(self, session_id, messages, user_id="local"):
        self.pending.append((session_id, list(messages), user_id))
        return 1


class FakeSkillLoader:
    def build_prompt(self):
        return "<available_skills></available_skills>", 0


class FakeCompactor:
    def __init__(self, summary="task summary"):
        self.summary = summary
        self.calls = []

    def summarize(
        self,
        messages,
        compact_count,
        raw_path,
        previous_summary="",
    ):
        self.calls.append({
            "messages": list(messages),
            "compact_count": compact_count,
            "raw_path": raw_path,
            "previous_summary": previous_summary,
        })
        return self.summary


def _counter(window=100000):
    return TokenCounter(model="fake-model", window_override=window)


def _manager(memory=None, window_turns=2, token_window=100000, compactor=None):
    return ContextManager(
        "IDENTITY",
        memory=memory,
        session_window_turns=window_turns,
        token_counter=_counter(token_window),
        compactor=compactor or FakeCompactor(),
    )


def _add_turns(manager, count, prefix="t"):
    for index in range(count):
        manager.append_user_message(f"{prefix}-user-{index}", turn_seq=index + 1)
        manager.append_assistant_message({"role": "assistant", "content": f"{prefix}-asst-{index}"})


def test_build_model_messages_injects_memory_and_skills(tmp_workspace, otel_exporter):
    memory = FakeMemory()
    manager = ContextManager("IDENTITY", memory=memory, skill_loader=FakeSkillLoader())
    manager.append_user_message("当前问题")
    ctx = RunContext.create("s", tmp_workspace)

    messages = manager.build_model_messages(ctx)

    assert messages[0]["role"] == "system"
    assert "IDENTITY" in messages[0]["content"]
    assert "MEMORY for 当前问题" in messages[0]["content"]
    assert "<available_skills>" in messages[0]["content"]
    assert messages[1] == {"role": "user", "content": "当前问题"}


def test_internal_turn_sequence_is_not_sent_to_model(tmp_workspace):
    manager = ContextManager("IDENTITY")
    manager.append_user_message("hello", turn_seq=9)
    messages = manager.build_model_messages(RunContext.create("s", tmp_workspace))
    assert messages[1] == {"role": "user", "content": "hello"}
    assert manager.history[0]["_turn_seq"] == 9


def test_memory_snapshot_is_reused_within_one_agent_run(tmp_workspace):
    memory = FakeMemory()
    manager = ContextManager("IDENTITY", memory=memory)
    manager.append_user_message("只检索一次")
    ctx = RunContext.create("s", tmp_workspace, user_id="alice")
    manager.build_model_messages(ctx)
    manager.build_model_messages(ctx)
    assert memory.build_calls == 1


def test_split_recent_turns_keeps_tool_pair_with_assistant():
    history = [
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": "data"},
        {"role": "user", "content": "new"},
        {"role": "assistant", "content": "done"},
    ]
    evicted, recent = split_recent_turns(history, keep_recent_turns=1)
    assert [item["role"] for item in evicted] == ["user", "assistant", "tool"]
    assert [item.get("content") for item in recent] == ["new", "done"]


def test_turn_window_evicts_only_complete_old_turns_to_pending(tmp_workspace):
    memory = FakeMemory()
    manager = _manager(memory=memory, window_turns=2)
    _add_turns(manager, 3)

    manager.build_model_messages(RunContext.create("session-x", tmp_workspace))

    assert [item["content"] for item in manager.history if item["role"] == "user"] == [
        "t-user-1",
        "t-user-2",
    ]
    assert memory.pending[0][0] == "session-x"
    assert [item["content"] for item in memory.pending[0][1]] == ["t-user-0", "t-asst-0"]
    assert manager.last_eviction_reason == "turn_window"
    assert manager.compact_count == 0


def test_token_budget_summarizes_every_turn_before_latest(tmp_workspace):
    memory = FakeMemory()
    compactor = FakeCompactor("goals, progress, blockers")
    manager = _manager(
        memory=memory,
        window_turns=20,
        token_window=80,
        compactor=compactor,
    )
    _add_turns(manager, 3, prefix="long-" + ("x" * 100))

    messages = manager.build_model_messages(RunContext.create("s", tmp_workspace))

    assert manager.last_compact_reason == "tokens"
    assert manager.compact_count == 1
    assert [item["content"] for item in manager.history] == [
        "long-" + ("x" * 100) + "-user-2",
        "long-" + ("x" * 100) + "-asst-2",
    ]
    assert len(compactor.calls[0]["messages"]) == 4
    assert memory.pending
    assert "goals, progress, blockers" in messages[0]["content"]
    assert "<conversation_summary" in messages[0]["content"]


def test_single_oversized_turn_is_kept(tmp_workspace):
    manager = _manager(window_turns=20, token_window=50)
    manager.append_user_message("read it", turn_seq=1)
    manager.append_assistant_message({"role": "assistant", "content": "word " * 4000})

    manager.build_model_messages(RunContext.create("s", tmp_workspace))

    assert manager.compact_count == 0
    assert manager.history[-1]["content"].startswith("word")


def test_manual_compact_moves_old_turns_to_pending(tmp_workspace):
    memory = FakeMemory()
    manager = _manager(memory=memory, window_turns=20)
    _add_turns(manager, 2)

    assert manager.compact_now(RunContext.create("s", tmp_workspace)) is True
    assert [item["content"] for item in manager.history] == ["t-user-1", "t-asst-1"]
    assert memory.pending


def test_compaction_failure_keeps_exact_history(tmp_workspace):
    class BrokenCompactor:
        def summarize(self, *args, **kwargs):
            raise RuntimeError("summary failed")

    manager = _manager(compactor=BrokenCompactor())
    _add_turns(manager, 2)
    before = list(manager.history)

    assert manager.compact_now(RunContext.create("s", tmp_workspace)) is False
    assert manager.history == before
    assert manager.working_summary == ""


def test_active_turn_does_not_consume_completed_turn_window(tmp_workspace):
    memory = FakeMemory()
    manager = _manager(memory=memory, window_turns=2)
    _add_turns(manager, 2)
    manager.append_user_message("current", turn_seq=3)

    manager.build_model_messages(RunContext.create("s", tmp_workspace))

    assert [item["content"] for item in manager.history if item["role"] == "user"] == [
        "t-user-0",
        "t-user-1",
        "current",
    ]
    assert memory.pending == []
