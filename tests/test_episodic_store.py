from datetime import datetime, timedelta
from pathlib import Path

from memory.episodic_store import EpisodicMemoryStore


def test_append_summary_persists_structured_episode(tmp_memory_dir: Path):
    store = EpisodicMemoryStore(tmp_memory_dir)
    records = store.append_summary(
        [{
            "body": "事件一",
            "stable_facts": ["稳定事实"],
            "lessons": ["经验教训"],
        }],
        reason="context_trim",
        session_id="s1",
        start_turn_seq=1,
        end_turn_seq=2,
    )

    assert len(records) == 1
    assert records[0].content == "事件一。"
    assert records[0].search_text == "事件一。"
    assert records[0].metadata["occurred_at"]
    assert records[0].metadata["stable_facts"] == ["稳定事实"]
    assert records[0].metadata["lessons"] == ["经验教训"]
    content = store.read_day()
    assert f"时间：{datetime.now().strftime('%Y-%m-%d')}" in content
    assert "事件：事件一。" in content
    assert "稳定事实：稳定事实" in content
    assert "经验：经验教训" in content


def test_append_summary_normalizes_and_filters(tmp_memory_dir: Path):
    store = EpisodicMemoryStore(tmp_memory_dir)
    items = store._normalize_items(["- 有内容", "  ", "无", "None", "正常"])
    assert items == [
        {"body": "有内容", "stable_facts": [], "lessons": []},
        {"body": "正常", "stable_facts": [], "lessons": []},
    ]


def test_append_summary_empty_returns_empty_records(tmp_memory_dir: Path):
    store = EpisodicMemoryStore(tmp_memory_dir)
    assert store.append_summary(["无", "None", "  "], reason="trim", session_id="s") == []
    assert store.read_day() == ""


def test_body_is_hard_limited(tmp_memory_dir: Path):
    store = EpisodicMemoryStore(tmp_memory_dir, body_max_chars=40)
    records = store.append_summary(["x" * 100], reason="trim", session_id="s")
    assert len(records[0].search_text) == 40


def test_body_cap_does_not_leave_a_broken_clause(tmp_memory_dir: Path):
    store = EpisodicMemoryStore(tmp_memory_dir, body_max_chars=40)
    records = store.append_summary(
        [{
            "body": "第一句描述背景。第二句描述处理过程，并说明最终结果已经落盘，后面还有不应被机械截断的补充文字。",
            "stable_facts": [],
            "lessons": [],
        }],
        reason="test",
        session_id="s",
    )

    body = records[0].search_text
    assert len(body) <= 40
    assert body.endswith(("。", "！", "？", "!", "?", "…"))


def test_read_recent_orders_old_to_new(tmp_memory_dir: Path):
    store = EpisodicMemoryStore(tmp_memory_dir)
    today = datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    store.append_summary(["今天事件"], reason="trim", session_id="s", day=today)
    store.append_summary(["昨天事件"], reason="trim", session_id="s", day=yesterday)
    combined = store.read_recent(days=2)
    assert combined.index("昨天事件") < combined.index("今天事件")


def test_list_records_returns_sqlite_canonical_records(tmp_memory_dir: Path):
    store = EpisodicMemoryStore(tmp_memory_dir)
    store.append_summary(["a"], reason="trim", session_id="s", day="2026-07-05")
    store.append_summary(["b"], reason="trim", session_id="s", day="2026-07-06")
    records = store.list_records()
    assert [record.search_text for record in records] == ["a。", "b。"]
    assert records[0].content == "a。"
    assert records[0].metadata["occurred_at"].startswith("2026-07-05")
    assert all(record.path.startswith("sqlite:episodic/") for record in records)


def test_time_is_metadata_and_rendered_separately_from_body(tmp_memory_dir: Path):
    store = EpisodicMemoryStore(tmp_memory_dir)
    record = store.append_summary(
        [{"body": "完成记忆索引迁移", "stable_facts": [], "lessons": []}],
        reason="test",
        session_id="s",
        day="2026-10-28",
    )[0]

    assert record.content == "完成记忆索引迁移。"
    assert record.search_text == "完成记忆索引迁移。"
    assert "2026-10-28" not in record.content
    assert record.metadata["occurred_at"] == "2026-10-28T00:00:00"
    assert store.render_record(record) == (
        "时间：2026-10-28 00:00:00\n事件：完成记忆索引迁移。"
    )


def test_short_body_is_normalized_to_a_complete_sentence(tmp_memory_dir: Path):
    store = EpisodicMemoryStore(tmp_memory_dir, body_max_chars=40)
    record = store.append_summary(
        ["模型没有输出句末标点"], reason="test", session_id="s"
    )[0]

    assert record.content == "模型没有输出句末标点。"
    assert len(record.content) <= 40
