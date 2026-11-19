import re

from memory.manager import MemoryManager
from tools.memory_tools import ReadMemoryTool, RememberMemoryTool, SearchMemoryTool


def test_remember_memory_tool_writes_semantic_memory(tmp_workspace, tmp_memory_dir):
    memory = MemoryManager(tmp_memory_dir, llm=None)
    try:
        result = RememberMemoryTool(tmp_workspace, memory).run({
            "operation": "ADD",
            "content": "用户喜欢先讨论方案",
        })
        assert result.ok
        assert "ADD applied" in result.content
        assert re.fullmatch(
            r"M-\d{8}T\d{9}-\d{3,}", result.metadata["memory_id"]
        )
        assert "用户喜欢先讨论方案" in memory.read_main_memory()
    finally:
        memory.close()


def test_search_memory_tool_returns_ranked_results(tmp_workspace, tmp_memory_dir):
    memory = MemoryManager(tmp_memory_dir, llm=None)
    try:
        records = memory.episodic_store.append_summary(
            ["用户讨论了测试覆盖率方案"], reason="trim", session_id="s"
        )
        for record in records:
            memory.index.upsert_record(record)
        result = SearchMemoryTool(tmp_workspace, memory).run({"query": "测试覆盖率", "max_results": 3})
        assert result.ok
        assert "测试覆盖率" in result.content
        assert "id=" not in result.content
    finally:
        memory.close()


def test_remember_memory_tool_updates_by_visible_id_and_reports_bad_id(tmp_workspace, tmp_memory_dir):
    memory = MemoryManager(tmp_memory_dir, llm=None)
    tool = RememberMemoryTool(tmp_workspace, memory)
    try:
        added = tool.run({"operation": "ADD", "content": "用户喜欢咖啡"})
        memory_id = added.metadata["memory_id"]
        updated = tool.run({
            "operation": "UPDATE",
            "memory_id": memory_id,
            "content": "用户现在喜欢茶",
        })
        missing = tool.run({
            "operation": "UPDATE",
            "memory_id": "M-0000000000",
            "content": "错误目标",
        })

        assert updated.ok
        assert f"[{memory_id}] 用户现在喜欢茶" in memory.read_main_memory()
        assert not missing.ok
        assert missing.error_code == "memory_not_found"
        assert missing.retryable is True
        assert memory_id in missing.details["available_memory_ids"]
    finally:
        memory.close()


def test_search_memory_tool_no_results(tmp_workspace, tmp_memory_dir):
    memory = MemoryManager(tmp_memory_dir, llm=None)
    try:
        result = SearchMemoryTool(tmp_workspace, memory).run({"query": "不存在", "max_results": 3})
        assert result.ok
        assert result.content == "(no relevant memories found)"
    finally:
        memory.close()


def test_read_memory_tool_reads_semantic_memory(tmp_workspace, tmp_memory_dir):
    memory = MemoryManager(tmp_memory_dir, llm=None)
    try:
        memory.remember("长期语义记忆", source="test")
        result = ReadMemoryTool(tmp_workspace, memory).run({})
        assert result.ok
        assert "长期语义记忆" in result.content
    finally:
        memory.close()
