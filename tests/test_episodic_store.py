from datetime import datetime, timedelta
from pathlib import Path

from memory.episodic_store import EpisodicMemoryStore


def test_append_summary_writes_block(tmp_memory_dir: Path):
    store = EpisodicMemoryStore(tmp_memory_dir)
    count = store.append_summary(["事件一", "事件二"], reason="context_trim", session_id="s1")
    assert count == 2
    content = store.read_day()
    assert "Trimmed Context" in content
    assert "s1" in content
    assert "- 事件一" in content
    assert "- 事件二" in content


def test_append_summary_normalizes_and_filters():
    store = EpisodicMemoryStore.__new__(EpisodicMemoryStore)
    items = store._normalize_items(["- 有内容", "  ", "无", "None", "正常"])
    assert items == ["有内容", "正常"]


def test_append_summary_empty_returns_zero(tmp_memory_dir: Path):
    store = EpisodicMemoryStore(tmp_memory_dir)
    assert store.append_summary(["无", "None", "  "], reason="trim", session_id="s") == 0
    # 没有内容不应创建残留块，read 为空或仅骨架
    assert "- " not in store.read_day()


def test_reason_title_mapping(tmp_memory_dir: Path):
    store = EpisodicMemoryStore(tmp_memory_dir)
    assert store._reason_title("context_trim") == "Trimmed Context"
    assert store._reason_title("overflow") == "Context Overflow Recovery"
    assert store._reason_title("daily_summary") == "Daily Summary"
    assert store._reason_title("weird") == "Session Notes"


def test_read_recent_orders_old_to_new(tmp_memory_dir: Path):
    store = EpisodicMemoryStore(tmp_memory_dir)
    today = datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    store.append_summary(["今天事件"], reason="trim", session_id="s", day=today)
    store.append_summary(["昨天事件"], reason="trim", session_id="s", day=yesterday)
    combined = store.read_recent(days=2)
    assert combined.index("昨天事件") < combined.index("今天事件")


def test_list_files_sorted(tmp_memory_dir: Path):
    store = EpisodicMemoryStore(tmp_memory_dir)
    store.append_summary(["a"], reason="trim", session_id="s", day="2026-07-05")
    store.append_summary(["b"], reason="trim", session_id="s", day="2026-07-06")
    files = [p.name for p in store.list_files()]
    assert files == sorted(files)
    assert "2026-07-05.md" in files
