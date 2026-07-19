from memory.curator import MemoryCurator
from tests.conftest import FakeLLM


def test_summarize_daily_without_llm_returns_empty():
    curator = MemoryCurator(llm=None)
    assert curator.summarize_daily(
        messages=[{"role": "user", "content": "hi"}],
        existing_daily_memory="",
        session_id="s",
        reason="context_trim",
    ) == []


def test_summarize_daily_parses_bullets():
    llm = FakeLLM(["- 事件一\n- 事件二"])
    curator = MemoryCurator(llm=llm)
    items = curator.summarize_daily(
        messages=[{"role": "user", "content": "做了两件事"}],
        existing_daily_memory="",
        session_id="s",
        reason="context_trim",
    )
    assert items == ["事件一", "事件二"]


def test_parse_bullets_filters_sentinel():
    curator = MemoryCurator()
    assert curator.parse_bullets("无") == []
    assert curator.parse_bullets("None") == []
    assert curator.parse_bullets("") == []
    assert curator.parse_bullets("- a\n\n- b") == ["a", "b"]


def test_parse_dream_output_splits_sections():
    curator = MemoryCurator()
    memory, dream = curator.parse_dream_output("[MEMORY]\n- x\n[DREAM]\n整理说明")
    assert "- x" in memory
    assert "整理说明" in dream


def test_parse_dream_output_missing_dream():
    curator = MemoryCurator()
    memory, dream = curator.parse_dream_output("[MEMORY]\n- only memory")
    assert "only memory" in memory
    assert dream == ""


def test_format_messages_differentiates_roles():
    curator = MemoryCurator()
    text = curator.format_messages([
        {"role": "user", "content": "问题"},
        {"role": "assistant", "content": "回答"},
        {"role": "assistant", "tool_calls": [{"function": {"name": "glob"}}]},
        {"role": "tool", "name": "glob", "content": "结果"},
    ])
    assert "user: 问题" in text
    assert "assistant: 回答" in text
    assert "tool_calls: glob" in text
    assert "tool:glob" in text


def test_summarize_tool_message_truncates_long():
    curator = MemoryCurator()
    long_text = "\n".join(f"line{i:04d}-padding-content" for i in range(50))
    assert len(long_text) > 500
    summary = curator._summarize_tool_message("bash", long_text)
    assert "..." in summary
    assert "line0000" in summary
    assert "line0049" in summary


def test_summarize_tool_message_short_kept():
    curator = MemoryCurator()
    assert curator._summarize_tool_message("bash", "short") == "short"
    assert curator._summarize_tool_message("bash", "") == ""
