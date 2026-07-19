from concurrent.futures import Future

from agent.context_manager import ContextManager
from agent.run_context import RunContext


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


def test_trim_history_triggers_async_memory_flush(tmp_workspace):
    memory = FakeMemory()
    manager = ContextManager("IDENTITY", max_history_messages=2, memory=memory, memory_flush_messages=9)
    manager.append_user_message("old1")
    manager.append_assistant_message({"role": "assistant", "content": "old2"})
    manager.append_user_message("new1")
    ctx = RunContext.create("session-x", tmp_workspace)

    messages = manager.build_model_messages(ctx)

    assert [m.get("content") for m in manager.history] == ["old2", "new1"]
    assert len(memory.flushed) == 1
    assert memory.flushed[0]["messages"] == [{"role": "user", "content": "old1"}]
    assert memory.flushed[0]["session_id"] == "session-x"
    assert memory.flushed[0]["max_messages"] == 9
    assert any(e["name"] == "memory_flush_dispatched" and "future" not in e for e in ctx.trace.events)
    assert messages[0]["role"] == "system"


def test_trim_records_async_memory_flush_result(tmp_workspace):
    memory = FutureMemory()
    manager = ContextManager("IDENTITY", max_history_messages=1, memory=memory)
    manager.append_user_message("old")
    manager.append_user_message("new")
    ctx = RunContext.create("session-x", tmp_workspace)

    manager.build_model_messages(ctx)
    memory.future.set_result({
        "ok": True,
        "skipped": False,
        "episodic_items": 1,
        "reason": "context_trim",
    })

    event = ctx.trace.events[-1]
    assert event["name"] == "memory_flush_completed"
    assert event["episodic_items"] == 1


def test_trim_drops_leading_tool_message(tmp_workspace):
    memory = FakeMemory()
    manager = ContextManager("IDENTITY", max_history_messages=2, memory=memory)
    manager.append_user_message("old")
    manager.append_tool_result("tool-1", "read_file", "orphan tool")
    manager.append_user_message("new")
    ctx = RunContext.create("s", tmp_workspace)

    manager.build_model_messages(ctx)

    assert manager.history == [{"role": "user", "content": "new"}]
    flushed_roles = [m["role"] for m in memory.flushed[0]["messages"]]
    assert flushed_roles == ["user", "tool"]


def test_latest_user_message_returns_last_user():
    manager = ContextManager("IDENTITY")
    history = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "second"},
    ]
    assert manager._latest_user_message(history) == "second"
    assert manager._latest_user_message([]) == ""
