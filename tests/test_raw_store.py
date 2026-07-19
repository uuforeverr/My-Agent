from pathlib import Path

from memory.raw_store import RawConversationStore


def test_append_and_read(tmp_memory_dir: Path):
    store = RawConversationStore(tmp_memory_dir)
    store.append_message("default", "user", "hi")
    store.append_message("default", "assistant", "hello", metadata={"k": 1})
    records = store.read_day("default")
    assert len(records) == 2
    assert records[0]["role"] == "user"
    assert records[0]["content"] == "hi"
    assert records[1]["metadata"] == {"k": 1}
    assert records[0]["created_at"]


def test_read_missing_returns_empty(tmp_memory_dir: Path):
    store = RawConversationStore(tmp_memory_dir)
    assert store.read_day("nobody") == []


def test_read_skips_bad_line(tmp_memory_dir: Path):
    store = RawConversationStore(tmp_memory_dir)
    path = store.append_message("s1", "user", "ok")
    with path.open("a", encoding="utf-8") as f:
        f.write("not-json\n")
    records = store.read_day("s1")
    assert len(records) == 1


def test_safe_session_id_sanitizes(tmp_memory_dir: Path):
    store = RawConversationStore(tmp_memory_dir)
    store.append_message("a/b c", "user", "x")
    dirs = [p.name for p in (tmp_memory_dir / "raw").iterdir()]
    assert "a_b_c" in dirs


def test_path_is_session_and_day_scoped(tmp_memory_dir: Path):
    store = RawConversationStore(tmp_memory_dir)
    path = store.append_message("default", "user", "x")
    assert path.parent.name == "default"
    assert path.suffix == ".jsonl"
