from concurrent.futures import Future

from agent.compactor import Compactor, split_recent_turns
from agent.context_manager import ContextManager
from agent.run_context import RunContext
from agent.token_counter import TokenCounter
from tests.conftest import FakeLLM


class FakeMemory:
    def __init__(self):
        self.flushed = []

    def build_prompt(self, query, limit=5):
        return f"MEMORY for {query}", 1

    def flush_from_messages(self, messages, reason, session_id, max_messages):
        self.flushed.append({
            "messages": messages,
            "reason": reason,
            "session_id": session_id,
            "max_messages": max_messages,
        })
        return {"ok": True, "async": True, "future": object()}


class FakeSkillLoader:
    def build_prompt(self):
        return "<available_skills></available_skills>", 0


class FutureMemory(FakeMemory):
    def __init__(self):
        super().__init__()
        self.future = Future()

    def flush_from_messages(self, messages, reason, session_id, max_messages):
        super().flush_from_messages(messages, reason, session_id, max_messages)
        return {
            "ok": True,
            "skipped": False,
            "async": True,
            "reason": reason,
            "future": self.future,
        }


def test_build_model_messages_injects_memory_and_skills(tmp_workspace):
    memory = FakeMemory()
    manager = ContextManager("IDENTITY", memory=memory, skill_loader=FakeSkillLoader())
    manager.append_user_message("当前问题")
    ctx = RunContext.create("s", tmp_workspace)

    messages = manager.build_model_messages(ctx)

    assert messages[0]["role"] == "system"
    system_content = messages[0]["content"]
    assert "IDENTITY" in system_content
    assert "Current date:" in system_content
    assert f"Workspace: {tmp_workspace.resolve()}" in system_content
    assert "Session ID: s" in system_content
    assert "MEMORY for 当前问题" in system_content
    assert "<available_skills>" in system_content
    assert messages[1] == {"role": "user", "content": "当前问题"}
    event = ctx.trace.events[-1]
    assert event["name"] == "context_built"
    assert event["runtime_injected"] is True
    assert event["memory_count"] == 1
    assert event["skill_catalog_injected"] is True
    span_kinds = [span.span_kind for span in ctx.trace.spans]
    assert span_kinds == [
        "context.build",
        "memory.inject",
        "skill.catalog.load",
        "skill.catalog.inject",
    ]
    context_span = ctx.trace.spans[0]
    assert all(span.parent_span_id == context_span.span_id for span in ctx.trace.spans[1:])


def test_build_model_messages_injects_runtime_metadata(tmp_workspace):
    manager = ContextManager("IDENTITY")
    manager.append_user_message("hello")
    ctx = RunContext.create(
        "cli:local",
        tmp_workspace,
        entrypoint="cli",
        channel_type="cli",
        user_id="local",
        is_group=False,
    )

    messages = manager.build_model_messages(ctx)

    system_content = messages[0]["content"]
    assert "entrypoint: cli" in system_content
    assert "channel_type: cli" in system_content
    assert "user_id: local" in system_content
    assert "is_group: False" in system_content


def test_latest_user_message_returns_last_user():
    manager = ContextManager("IDENTITY")
    history = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "second"},
    ]
    assert manager._latest_user_message(history) == "second"
    assert manager._latest_user_message([]) == ""


def _counter(window=100000):
    return TokenCounter(model="fake-model", window_override=window)


def _manager(memory=None, max_history=2, keep=1, window=100000, llm="SUMMARY", **kwargs):
    if isinstance(llm, str):
        llm = FakeLLM(llm)
    return ContextManager(
        "IDENTITY",
        max_history_messages=max_history,
        memory=memory,
        keep_recent_turns=keep,
        token_counter=_counter(window),
        compactor=Compactor(llm, "compact prompt") if llm is not None else None,
        memory_flush_messages=kwargs.pop("memory_flush_messages", 9),
        **kwargs,
    )


def _add_turns(manager, count, prefix="t"):
    for index in range(count):
        manager.append_user_message(f"{prefix}-user-{index}")
        manager.append_assistant_message({"role": "assistant", "content": f"{prefix}-asst-{index}"})


def test_split_recent_turns_keeps_tool_pair_with_assistant():
    history = [
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": "data"},
        {"role": "user", "content": "new"},
        {"role": "assistant", "content": "done"},
    ]
    to_compact, recent = split_recent_turns(history, keep_recent_turns=1)
    assert to_compact[0]["content"] == "old"
    assert to_compact[1]["tool_calls"][0]["id"] == "c1"
    assert to_compact[2]["role"] == "tool"
    assert [m.get("content") for m in recent] == ["new", "done"]


def test_count_trigger_replaces_old_turns_with_summary(tmp_workspace):
    memory = FakeMemory()
    llm = FakeLLM("goals: none")
    manager = _manager(memory=memory, max_history=3, keep=1, window=100000, llm=llm)
    _add_turns(manager, 3)
    ctx = RunContext.create("session-x", tmp_workspace)

    messages = manager.build_model_messages(ctx)

    assert manager.history[0]["role"] == "user"
    assert "<conversation_summary compact_count=\"1\">" in manager.history[0]["content"]
    assert "goals: none" in manager.history[0]["content"]
    assert "data/memory/raw/session-x/" in manager.history[0]["content"]
    assert manager.history[-1]["content"] == "t-asst-2"
    assert manager.compact_count == 1
    assert manager.last_compact_reason == "count"
    assert memory.flushed[0]["reason"] == "context_compact"
    assert [m["content"] for m in memory.flushed[0]["messages"]] == [
        "t-user-0", "t-asst-0", "t-user-1", "t-asst-1",
    ]
    assert llm.calls and llm.calls[0][0]["role"] == "system"
    compact_span = next(span for span in ctx.trace.spans if span.span_kind == "context.compact")
    assert compact_span.attributes["reason"] == "count"
    assert messages[0]["role"] == "system"


def test_token_trigger_without_count_over(tmp_workspace):
    memory = FakeMemory()
    manager = _manager(memory=memory, max_history=40, keep=1, window=80, llm="token-summary")
    _add_turns(manager, 2)
    ctx = RunContext.create("s", tmp_workspace)

    manager.build_model_messages(ctx)

    assert manager.last_compact_reason == "tokens"
    assert manager.history[0]["content"].startswith("<conversation_summary")
    assert "token-summary" in manager.history[0]["content"]
    assert memory.flushed


def test_both_triggers_compact_once(tmp_workspace):
    manager = _manager(max_history=2, keep=1, window=80, llm="both")
    _add_turns(manager, 3)
    ctx = RunContext.create("s", tmp_workspace)

    manager.build_model_messages(ctx)

    assert manager.compact_count == 1
    assert manager.last_compact_reason == "count"


def test_neither_trigger_keeps_history(tmp_workspace):
    memory = FakeMemory()
    manager = _manager(memory=memory, max_history=40, keep=3, window=100000, llm="should-not-run")
    manager.append_user_message("hello")
    ctx = RunContext.create("s", tmp_workspace)

    manager.build_model_messages(ctx)

    assert manager.history == [{"role": "user", "content": "hello"}]
    assert manager.compact_count == 0
    assert memory.flushed == []
    assert all(span.span_kind != "context.compact" for span in ctx.trace.spans)


def test_compact_flush_records_async_result(tmp_workspace):
    memory = FutureMemory()
    manager = _manager(memory=memory, max_history=1, keep=1, window=100000)
    _add_turns(manager, 2)
    ctx = RunContext.create("session-x", tmp_workspace)

    manager.build_model_messages(ctx)
    memory.future.set_result({
        "ok": True,
        "skipped": False,
        "episodic_items": 1,
        "reason": "context_compact",
    })

    event = ctx.trace.events[-1]
    assert event["name"] == "memory_flush_completed"
    assert event["episodic_items"] == 1


def test_compact_failure_keeps_history(tmp_workspace):
    class BoomLLM:
        def chat(self, messages, tools=None):
            raise RuntimeError("boom")

    memory = FakeMemory()
    original = [
        {"role": "user", "content": "u0"},
        {"role": "assistant", "content": "a0"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
    ]
    manager = _manager(memory=memory, max_history=2, keep=1, window=100000, llm=BoomLLM())
    manager.history = list(original)
    ctx = RunContext.create("s", tmp_workspace)

    manager.build_model_messages(ctx)

    assert manager.history == original
    assert manager.compact_count == 0
    assert memory.flushed == []
    compact_span = next(span for span in ctx.trace.spans if span.span_kind == "context.compact")
    assert compact_span.status == "error"


def test_empty_summary_keeps_history(tmp_workspace):
    manager = _manager(max_history=2, keep=1, window=100000, llm=FakeLLM({"role": "assistant", "content": ""}))
    _add_turns(manager, 2)
    original = list(manager.history)
    ctx = RunContext.create("s", tmp_workspace)

    manager.build_model_messages(ctx)

    assert manager.history == original
    assert manager.compact_count == 0


def test_token_over_in_recent_turns_keeps_history(tmp_workspace):
    manager = _manager(max_history=40, keep=3, window=50, llm="unused")
    huge = "word " * 4000
    manager.append_user_message("read it")
    manager.append_assistant_message({
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": "c1", "function": {"name": "bash"}}],
    })
    manager.append_tool_result("c1", "bash", huge)
    ctx = RunContext.create("s", tmp_workspace)

    manager.build_model_messages(ctx)

    assert manager.compact_count == 0
    assert manager.history[-1]["content"] == huge
    assert "[truncated:" not in manager.history[-1]["content"]


def test_manual_compact_without_old_turns_is_noop(tmp_workspace):
    manager = _manager(max_history=40, keep=3, window=100000)
    manager.append_user_message("only")
    ctx = RunContext.create("s", tmp_workspace)

    assert manager.compact_now(ctx) is False
    assert manager.compact_count == 0


def test_manual_compact_ignores_thresholds(tmp_workspace):
    manager = _manager(max_history=40, keep=1, window=100000, llm="manual-summary")
    _add_turns(manager, 2)
    ctx = RunContext.create("cli:local", tmp_workspace)

    assert manager.compact_now(ctx) is True
    assert manager.last_compact_reason == "manual"
    assert "manual-summary" in manager.history[0]["content"]
