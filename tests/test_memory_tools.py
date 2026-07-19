from memory.manager import MemoryManager
from tools.memory_tools import ReadMemoryTool, RememberMemoryTool, SearchMemoryTool


def test_remember_memory_tool_writes_semantic_memory(tmp_workspace, tmp_memory_dir):
    memory = MemoryManager(tmp_memory_dir, llm=None)
    try:
        result = RememberMemoryTool(tmp_workspace, memory).run({"content": "用户喜欢先讨论方案"})
        assert result.ok
        assert "saved" in result.content.lower()
        assert "用户喜欢先讨论方案" in memory.read_main_memory()
    finally:
        memory.close()


def test_search_memory_tool_returns_ranked_results(tmp_workspace, tmp_memory_dir):
    memory = MemoryManager(tmp_memory_dir, llm=None)
    try:
        memory.episodic_store.append_summary(["用户讨论了测试覆盖率方案"], reason="trim", session_id="s")
        memory._reindex_today_episodic()
        result = SearchMemoryTool(tmp_workspace, memory).run({"query": "测试覆盖率", "max_results": 3})
        assert result.ok
        assert "测试覆盖率" in result.content
        assert "score=" in result.content
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
