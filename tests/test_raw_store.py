from pathlib import Path

from memory.raw_store import RawConversationStore


def _turn(store: RawConversationStore, session: str, index: int) -> int:
    receipt = store.append_message(session, "user", f"u{index}")
    store.append_message(
        session,
        "assistant",
        f"a{index}",
        turn_seq=receipt.turn_seq,
    )
    return receipt.turn_seq


def test_async_append_and_read_complete_turn(tmp_memory_dir: Path):
    store = RawConversationStore(tmp_memory_dir, flush_interval_ms=5)
    seq = _turn(store, "default", 1)

    turns = store.read_recent_turns("default", 20)

    assert [turn["turn_seq"] for turn in turns] == [seq]
    assert [item["content"] for item in turns[0]["messages"]] == ["u1", "a1"]
    store.close()


def test_incomplete_turn_is_not_hydrated(tmp_memory_dir: Path):
    store = RawConversationStore(tmp_memory_dir, flush_interval_ms=5)
    store.append_message("s1", "user", "unfinished")

    assert store.read_recent_turns("s1", 20) == []
    store.close()


def test_recent_n_turns_are_returned_in_chronological_order(tmp_memory_dir: Path):
    store = RawConversationStore(tmp_memory_dir, flush_interval_ms=5)
    for index in range(5):
        _turn(store, "s1", index)

    turns = store.read_recent_turns("s1", 3)

    assert [turn["turn_seq"] for turn in turns] == [3, 4, 5]
    assert [turn["messages"][0]["content"] for turn in turns] == ["u2", "u3", "u4"]
    store.close()


def test_turn_keeps_assistant_tool_and_final_message(tmp_memory_dir: Path):
    store = RawConversationStore(tmp_memory_dir, flush_interval_ms=5)
    receipt = store.append_message("s1", "user", "read")
    store.append_message(
        "s1",
        "assistant",
        "",
        metadata={"tool_calls": [{"id": "c1"}]},
        turn_seq=receipt.turn_seq,
    )
    store.append_message("s1", "tool", "result", turn_seq=receipt.turn_seq)
    store.append_message("s1", "assistant", "done", turn_seq=receipt.turn_seq)

    records = store.read_session("s1")

    assert [item["role"] for item in records] == ["user", "assistant", "tool", "assistant"]
    assert [item["message_seq"] for item in records] == [1, 2, 3, 4]
    store.close()


def test_pending_recovery_includes_compacted_range_even_inside_recent_window(
    tmp_memory_dir: Path,
):
    store = RawConversationStore(tmp_memory_dir, flush_interval_ms=5)
    for index in range(5):
        _turn(store, "s1", index)

    turns = store.read_pending_turns(
        "s1",
        after_turn_seq=0,
        exclude_recent=20,
        through_turn_seq=4,
    )

    assert [turn["turn_seq"] for turn in turns] == [1, 2, 3, 4]
    store.close()


def test_legacy_jsonl_is_imported_once_without_deletion(tmp_memory_dir: Path):
    session_dir = tmp_memory_dir / "raw" / "cli_local_cid"
    session_dir.mkdir(parents=True, exist_ok=True)
    path = session_dir / "2026-07-05.jsonl"
    path.write_text(
        '{"created_at":"2026-07-05T10:00:00","session_id":"cli:local:cid","role":"user","content":"day1"}\n'
        '{"created_at":"2026-07-05T10:00:01","session_id":"cli:local:cid","role":"assistant","content":"day1 answer"}\n',
        encoding="utf-8",
    )

    store = RawConversationStore(tmp_memory_dir)

    assert [item["content"] for item in store.read_session("cli:local:cid")] == [
        "day1",
        "day1 answer",
    ]
    assert path.exists()
    store.close()
