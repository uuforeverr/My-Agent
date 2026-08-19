from pathlib import Path

from agent.run_context import RunContext
from memory.manager import MemoryManager
from scheduler.task_store import TaskStore
from tools.registry import ToolRegistry


def test_registry_schemas_and_unknown_tool(tmp_workspace, tmp_memory_dir):
    memory = MemoryManager(tmp_memory_dir, llm=None)
    try:
        registry = ToolRegistry(tmp_workspace, memory, bash_timeout=3)
        names = {schema["function"]["name"] for schema in registry.schemas()}
        assert {"bash", "glob", "read_file", "write_file", "edit_file", "remember_memory", "memory_search", "read_memory"} <= names
        result = registry.run("missing", {})
        assert result.ok is False
        assert "Unknown tool" in result.content
    finally:
        memory.close()


def test_registry_registers_web_search_only_when_key_exists(tmp_workspace, tmp_memory_dir):
    memory = MemoryManager(tmp_memory_dir, llm=None)
    try:
        no_key_registry = ToolRegistry(tmp_workspace, memory)
        no_key_names = {schema["function"]["name"] for schema in no_key_registry.schemas()}
        assert "web_search" not in no_key_names

        registry = ToolRegistry(tmp_workspace, memory, qianfan_api_key="key")
        names = {schema["function"]["name"] for schema in registry.schemas()}
        assert "web_search" in names
    finally:
        memory.close()


def test_registry_parses_json_arguments(tmp_workspace, tmp_memory_dir):
    memory = MemoryManager(tmp_memory_dir, llm=None)
    try:
        registry = ToolRegistry(tmp_workspace, memory)
        result = registry.run("write_file", '{"path":"a.txt","content":"hello"}')
        assert result.ok
        assert (tmp_workspace / "a.txt").read_text(encoding="utf-8") == "hello"
    finally:
        memory.close()


def test_registry_rejects_invalid_json(tmp_workspace, tmp_memory_dir):
    memory = MemoryManager(tmp_memory_dir, llm=None)
    try:
        registry = ToolRegistry(tmp_workspace, memory)
        result = registry.run("read_file", "{bad-json")
        assert result.ok is False
        assert "Invalid JSON" in result.content
    finally:
        memory.close()


def test_registry_records_tool_span_for_success(tmp_workspace, tmp_memory_dir):
    memory = MemoryManager(tmp_memory_dir, llm=None)
    try:
        registry = ToolRegistry(tmp_workspace, memory)
        context = RunContext.create(session_id="cli:local", workspace=Path(tmp_workspace))

        result = registry.run("write_file", '{"path":"span.txt","content":"hello"}', context)

        assert result.ok
        span = context.trace.spans[0]
        assert span.span_kind == "tool.execute"
        assert span.attributes["tool_name"] == "write_file"
        assert span.attributes["argument_keys"] == ["content", "path"]
        assert span.attributes["ok"] is True
        assert span.status == "success"
    finally:
        memory.close()


def test_registry_records_skill_activate_when_reading_skill_file(tmp_workspace, tmp_memory_dir):
    skill_dir = tmp_workspace / "skills" / "code-review"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Code Review\n", encoding="utf-8")
    memory = MemoryManager(tmp_memory_dir, llm=None)
    try:
        registry = ToolRegistry(tmp_workspace, memory)
        context = RunContext.create(session_id="cli:local", workspace=Path(tmp_workspace))

        result = registry.run("read_file", {"path": "skills/code-review/SKILL.md"}, context)

        assert result.ok
        tool_span, activate_span = context.trace.spans
        assert tool_span.span_kind == "tool.execute"
        assert activate_span.span_kind == "skill.activate"
        assert activate_span.parent_span_id == tool_span.span_id
        assert activate_span.attributes["skill_name"] == "code-review"
        assert Path(activate_span.attributes["file_path"]).as_posix() == "skills/code-review/SKILL.md"
        assert activate_span.attributes["activation_source"] == "read_file"
        assert activate_span.attributes["ok"] is True
        assert activate_span.status == "success"
    finally:
        memory.close()


def test_registry_does_not_record_skill_activate_for_normal_read(tmp_workspace, tmp_memory_dir):
    (tmp_workspace / "notes.md").write_text("# Notes\n", encoding="utf-8")
    memory = MemoryManager(tmp_memory_dir, llm=None)
    try:
        registry = ToolRegistry(tmp_workspace, memory)
        context = RunContext.create(session_id="cli:local", workspace=Path(tmp_workspace))

        result = registry.run("read_file", {"path": "notes.md"}, context)

        assert result.ok
        assert [span.span_kind for span in context.trace.spans] == ["tool.execute"]
    finally:
        memory.close()


def test_registry_records_tool_span_for_invalid_json(tmp_workspace, tmp_memory_dir):
    memory = MemoryManager(tmp_memory_dir, llm=None)
    try:
        registry = ToolRegistry(tmp_workspace, memory)
        context = RunContext.create(session_id="cli:local", workspace=Path(tmp_workspace))

        result = registry.run("read_file", "{bad-json", context)

        assert result.ok is False
        span = context.trace.spans[0]
        assert span.span_kind == "tool.execute"
        assert span.status == "error"
        assert span.attributes["tool_name"] == "read_file"
        assert span.attributes["error_type"] == "invalid_json"
    finally:
        memory.close()


def test_registry_registers_scheduler_only_when_store_provided(tmp_workspace, tmp_memory_dir, tmp_path):
    memory = MemoryManager(tmp_memory_dir, llm=None)
    try:
        no_store = ToolRegistry(tmp_workspace, memory)
        assert "scheduler" not in {s["function"]["name"] for s in no_store.schemas()}

        store = TaskStore(tmp_path / "scheduler" / "tasks.json")
        with_store = ToolRegistry(tmp_workspace, memory, scheduler_store=store)
        assert "scheduler" in {s["function"]["name"] for s in with_store.schemas()}
    finally:
        memory.close()


def test_registry_hides_scheduler_in_scheduled_task_context(tmp_workspace, tmp_memory_dir, tmp_path):
    memory = MemoryManager(tmp_memory_dir, llm=None)
    try:
        store = TaskStore(tmp_path / "scheduler" / "tasks.json")
        registry = ToolRegistry(tmp_workspace, memory, scheduler_store=store)

        scheduled_ctx = RunContext.create(
            session_id="scheduler:cli:local:abcd1234",
            workspace=Path(tmp_workspace),
            is_scheduled_task=True,
        )
        names = {s["function"]["name"] for s in registry.schemas(scheduled_ctx)}
        assert "scheduler" not in names

        # Even if the model somehow calls it, run() must refuse.
        result = registry.run("scheduler", {"action": "list"}, scheduled_ctx)
        assert result.ok is False
        assert "not available" in result.content

        # A normal context still exposes and runs scheduler.
        normal_ctx = RunContext.create(
            session_id="cli:local",
            workspace=Path(tmp_workspace),
        )
        assert "scheduler" in {s["function"]["name"] for s in registry.schemas(normal_ctx)}
    finally:
        memory.close()
