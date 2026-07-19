from pathlib import Path

from memory.manager import MemoryManager
from tests.conftest import FakeLLM


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
    # 三段都应存在，daily 文件未被并发写坏
    assert daily.count("## Trimmed Context") == 3
    manager.close()


def test_deep_dream_overwrites_semantic(tmp_memory_dir: Path):
    dream_output = (
        "[MEMORY]\n# Semantic Memory\n\n- 蒸馏后的稳定结论\n\n[DREAM]\n整理了今天的情景记忆。"
    )
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
