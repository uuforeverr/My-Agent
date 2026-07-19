from pathlib import Path

from memory.semantic_store import SemanticMemoryStore


def test_init_writes_skeleton(tmp_memory_dir: Path):
    store = SemanticMemoryStore(tmp_memory_dir)
    content = store.read()
    assert "# Semantic Memory" in content
    assert "## User Preferences" in content
    assert "## Project Decisions" in content


def test_remember_appends_to_manual_notes(tmp_memory_dir: Path):
    store = SemanticMemoryStore(tmp_memory_dir)
    msg = store.remember("用户偏好简洁回答", source="test")
    assert "saved" in msg.lower()
    content = store.read()
    assert "## Manual Notes" in content
    assert "用户偏好简洁回答" in content
    assert "source: test" in content


def test_remember_dedup(tmp_memory_dir: Path):
    store = SemanticMemoryStore(tmp_memory_dir)
    store.remember("同一条记忆", source="a")
    msg = store.remember("同一条记忆", source="b")
    assert "already exists" in msg.lower()
    assert store.read().count("同一条记忆") == 1


def test_remember_empty_rejected(tmp_memory_dir: Path):
    store = SemanticMemoryStore(tmp_memory_dir)
    msg = store.remember("   ", source="x")
    assert "empty" in msg.lower()


def test_overwrite_replaces_all(tmp_memory_dir: Path):
    store = SemanticMemoryStore(tmp_memory_dir)
    store.remember("旧记忆", source="x")
    store.overwrite("# Semantic Memory\n\n- 新的整理结果")
    content = store.read()
    assert "新的整理结果" in content
    assert "旧记忆" not in content


def test_overwrite_empty_falls_back(tmp_memory_dir: Path):
    store = SemanticMemoryStore(tmp_memory_dir)
    store.overwrite("   ")
    assert "# Semantic Memory" in store.read()
