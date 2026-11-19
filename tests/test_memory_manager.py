from pathlib import Path

from memory.embeddings import EmbeddingStatus
from memory.manager import MemoryManager
from tests.conftest import FakeLLM


class _WarmEmbedder:
    model_id = "warm-test"
    dimensions = 2
    available = True
    error = ""

    def __init__(self):
        self.warmup_calls = 0

    def warmup(self):
        self.warmup_calls += 1
        return EmbeddingStatus("fake", self.model_id, True, self.dimensions, warmup_ms=1.0)

    def encode(self, texts):
        return [[1.0, 0.0] for _ in texts]


def test_build_prompt_semantic_full_and_episodic_topk(tmp_memory_dir: Path):
    llm = FakeLLM(["- 事件：用户确认异步 flush"])
    manager = MemoryManager(tmp_memory_dir, llm=llm)
    manager.remember("用户偏好中文回答", source="test")

    stats = manager.flush_from_messages(
        [{"role": "user", "content": "把 flush 改成异步"}],
        session_id="s",
        max_messages=0,
    )
    stats["future"].result(timeout=5)

    prompt, count = manager.build_prompt("异步", limit=5)
    assert "### Semantic memory" in prompt
    assert "用户偏好中文回答" in prompt
    assert "### Episodic memory" in prompt
    assert count >= 2
    manager.close()


def test_semantic_not_indexed(tmp_memory_dir: Path):
    manager = MemoryManager(tmp_memory_dir, llm=FakeLLM())
    manager.remember("独一无二的语义短语 zebrakeyword", source="test")
    results = manager.search("zebrakeyword", limit=5)
    assert all(r.record.source != "semantic" for r in results)
    manager.close()


def test_episodic_index_uses_body_only_but_recall_hydrates_metadata(tmp_memory_dir: Path):
    manager = MemoryManager(tmp_memory_dir, llm=FakeLLM())
    records = manager.episodic_store.append_summary(
        [{
            "body": "完成数据库迁移",
            "stable_facts": ["仅存在于元数据的稀有词星河锚点"],
            "lessons": ["先做备份"],
        }],
        reason="test",
        session_id="s",
    )
    manager.index.upsert_record(records[0])

    with manager.state_database.read() as conn:
        indexed_text = conn.execute(
            "SELECT text FROM memory_chunks WHERE id = ?",
            (records[0].id,),
        ).fetchone()["text"]
    recalled = manager.search("数据库迁移", limit=1)

    assert indexed_text == "完成数据库迁移。"
    assert recalled
    assert recalled[0].record.content == "完成数据库迁移。"
    assert recalled[0].record.metadata["stable_facts"] == ["仅存在于元数据的稀有词星河锚点"]
    assert "稳定事实：仅存在于元数据的稀有词星河锚点" in (
        manager.episodic_store.render_record(recalled[0].record)
    )
    assert manager.search("星河锚点", limit=5) == []
    manager.close()


def test_flush_is_async_and_persists(tmp_memory_dir: Path):
    llm = FakeLLM(["- 用户要求把 flush 放到后台线程"])
    manager = MemoryManager(tmp_memory_dir, llm=llm)
    stats = manager.flush_from_messages(
        [{"role": "user", "content": "flush 异步化"}],
        session_id="s",
        max_messages=0,
    )
    assert stats["async"] is True
    assert "future" in stats
    result = stats["future"].result(timeout=5)
    assert result["ok"] is True
    assert result["episodic_items"] == 1

    daily = manager.read_today_episodic_memory()
    assert "后台线程" in daily
    assert manager.search("后台线程", limit=5)
    manager.close()


def test_flush_empty_messages_skipped(tmp_memory_dir: Path):
    manager = MemoryManager(tmp_memory_dir, llm=FakeLLM())
    stats = manager.flush_from_messages([], session_id="s")
    assert stats["skipped"] is True
    assert stats["async"] is False
    manager.close()


def test_compaction_state_round_trips_through_sqlite(tmp_memory_dir: Path):
    manager = MemoryManager(tmp_memory_dir, llm=FakeLLM())
    manager.save_compaction_state(
        "session-a",
        summary="goal and blocker",
        compact_count=2,
        summarized_through_turn_seq=7,
        raw_path="sqlite:test#session-a",
    )

    state = manager.load_compaction_state("session-a")

    assert state["summary"] == "goal and blocker"
    assert state["compact_count"] == 2
    assert state["summarized_through_turn_seq"] == 7
    assert state["raw_path"] == "sqlite:test#session-a"
    manager.close()


def test_flush_reports_no_summary_items(tmp_memory_dir: Path):
    manager = MemoryManager(tmp_memory_dir, llm=FakeLLM("无"))
    stats = manager.flush_from_messages(
        [{"role": "user", "content": "闲聊一句"}],
        session_id="s",
        max_messages=0,
    )

    result = stats["future"].result(timeout=5)

    assert result["ok"] is True
    assert result["skipped"] is True
    assert result["episodic_items"] == 0
    assert result["skip_reason"] == "no_summary_items"
    manager.close()


def test_concurrent_flush_serialized(tmp_memory_dir: Path):
    llm = FakeLLM([
        "- 批次一事件",
        "- 批次二事件",
        "- 批次三事件",
    ])
    manager = MemoryManager(tmp_memory_dir, llm=llm)
    futures = []
    for i in range(3):
        stats = manager.flush_from_messages(
            [{"role": "user", "content": f"batch {i}"}],
            session_id="s",
            max_messages=0,
        )
        futures.append(stats["future"])
    for f in futures:
        f.result(timeout=5)

    daily = manager.read_today_episodic_memory()
    assert "批次一事件" in daily
    assert "批次二事件" in daily
    assert "批次三事件" in daily
    manager.close()


def test_deep_dream_applies_semantic_operations(tmp_memory_dir: Path):
    dream_output = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": "dream-call",
            "function": {
                "name": "apply_memory_operations",
                "arguments": (
                    '{"operations":[{"op":"ADD","content":"蒸馏后的稳定结论"}],'
                    '"dream":"整理了今天的情景记忆。"}'
                ),
            },
        }],
    }
    llm = FakeLLM([
        "- 今天用户敲定了记忆分层方案",
        dream_output,
    ])
    manager = MemoryManager(tmp_memory_dir, llm=llm)
    manager.flush_from_messages(
        [{"role": "user", "content": "确定分层方案"}],
        session_id="s",
        max_messages=0,
    )["future"].result(timeout=5)

    result = manager.deep_dream(lookback_days=1)
    assert result["ok"] is True
    assert "蒸馏后的稳定结论" in manager.read_main_memory()
    assert Path(result["dream_path"]).exists()
    manager.close()


def test_deep_dream_skips_without_episodic(tmp_memory_dir: Path):
    manager = MemoryManager(tmp_memory_dir, llm=FakeLLM(["[MEMORY]\nx\n[DREAM]\ny"]))
    result = manager.deep_dream(lookback_days=1)
    assert result["ok"] is False
    assert result["skipped"] is True
    manager.close()


def test_deep_dream_retries_invalid_function_call_with_error(tmp_memory_dir: Path):
    valid_call = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": "dream-retry",
            "function": {
                "name": "apply_memory_operations",
                "arguments": '{"operations":[{"op":"ADD","content":"重试后的事实"}]}',
            },
        }],
    }
    llm = FakeLLM([
        "- 一次需要进入 Dream 的事件",
        {"role": "assistant", "content": "错误地输出了普通文本"},
        valid_call,
    ])
    manager = MemoryManager(tmp_memory_dir, llm=llm)
    manager.flush_from_messages(
        [{"role": "user", "content": "需要记忆的事实"}],
        session_id="s",
        max_messages=0,
    )["future"].result(timeout=5)

    result = manager.deep_dream()

    assert result["ok"] is True
    assert "重试后的事实" in manager.read_main_memory()
    retry_prompt = llm.calls[-1][-1]["content"]
    assert "上一次 Function Call 校验错误" in retry_prompt
    assert "expected exactly one" in retry_prompt
    manager.close()


def test_close_idempotent_and_blocks_new_flush(tmp_memory_dir: Path):
    manager = MemoryManager(tmp_memory_dir, llm=FakeLLM())
    manager.close(wait=True)
    manager.close(wait=True)
    stats = manager.flush_from_messages(
        [{"role": "user", "content": "x"}],
        session_id="s",
    )
    assert stats["skipped"] is True
    assert stats["reason"] == "manager_closed"


def test_embedding_model_is_warmed_once_and_reused(tmp_memory_dir: Path):
    embedder = _WarmEmbedder()
    manager = MemoryManager(tmp_memory_dir, llm=FakeLLM(), embedder=embedder)

    manager.search("first", user_id="alice")
    manager.search("second", user_id="alice")

    assert embedder.warmup_calls == 1
    assert manager.embedding_status.available is True
    manager.close()


def test_semantic_memory_is_shared_across_channel_user_ids(tmp_memory_dir: Path):
    manager = MemoryManager(tmp_memory_dir, llm=FakeLLM())
    manager.remember("Alice preference", user_id="alice")
    manager.remember("Bob preference", user_id="bob")

    alice_prompt, _ = manager.build_prompt("preference", user_id="alice")
    bob_prompt, _ = manager.build_prompt("preference", user_id="bob")

    assert "Alice preference" in alice_prompt and "Bob preference" in alice_prompt
    assert "Alice preference" in bob_prompt and "Bob preference" in bob_prompt
    manager.close()
