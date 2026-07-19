from pathlib import Path

from memory.index import MemoryIndex


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_chunk_markdown_splits_by_h2(tmp_path: Path):
    index = MemoryIndex(tmp_path / "index.db")
    content = "# Title\n\n## A\nalpha\n\n## B\nbeta"
    chunks = index._chunk_markdown(content)
    texts = [c[2] for c in chunks]
    assert any("## A" in t and "alpha" in t for t in texts)
    assert any("## B" in t and "beta" in t for t in texts)
    index.close()


def test_rebuild_and_search_hits(tmp_path: Path):
    root = tmp_path / "memory"
    daily = root / "daily" / "2026-07-06.md"
    _write(daily, "## Trimmed Context\n\n- 用户要求异步 flush 机制")
    index = MemoryIndex(root / "index.db")
    index.rebuild([(daily, "episodic")], root=root)
    results = index.search("异步 flush", limit=5)
    assert results
    assert any("异步" in r.record.content for r in results)
    index.close()


def test_reindex_file_incremental(tmp_path: Path):
    root = tmp_path / "memory"
    d1 = root / "daily" / "2026-07-05.md"
    d2 = root / "daily" / "2026-07-06.md"
    _write(d1, "## Day1\n\n- 关键词 apple")
    _write(d2, "## Day2\n\n- 关键词 banana")
    index = MemoryIndex(root / "index.db")
    index.rebuild([(d1, "episodic"), (d2, "episodic")], root=root)

    _write(d2, "## Day2\n\n- 关键词 cherry")
    index.reindex_file(d2, source_type="episodic", root=root)

    # 旧内容 banana 已从索引移除：不会作为高分命中出现。
    banana_hits = [r for r in index.search("banana", limit=5) if r.score > 0.05]
    assert banana_hits == []
    # 新内容 cherry 与未受影响的 apple 仍可高分命中。
    assert any(r.score > 0.05 for r in index.search("cherry", limit=5))
    assert any(r.score > 0.05 for r in index.search("apple", limit=5))
    index.close()


def test_remove_source(tmp_path: Path):
    root = tmp_path / "memory"
    daily = root / "daily" / "2026-07-06.md"
    _write(daily, "## X\n\n- 需要删除的记忆")
    index = MemoryIndex(root / "index.db")
    index.rebuild([(daily, "episodic")], root=root)
    assert index.search("删除", limit=5)
    index.remove_source("daily/2026-07-06.md")
    assert index.search("删除", limit=5) == []
    index.close()


def test_search_empty_query_limit_zero(tmp_path: Path):
    root = tmp_path / "memory"
    daily = root / "daily" / "2026-07-06.md"
    _write(daily, "## X\n\n- something")
    index = MemoryIndex(root / "index.db")
    index.rebuild([(daily, "episodic")], root=root)
    assert index.search("anything", limit=0) == []
    index.close()


def test_search_degrades_without_fts(tmp_path: Path):
    root = tmp_path / "memory"
    daily = root / "daily" / "2026-07-06.md"
    _write(daily, "## X\n\n- fallback content here")
    index = MemoryIndex(root / "index.db")
    index.fts_available = False
    index.rebuild([(daily, "episodic")], root=root)
    results = index.search("fallback", limit=5)
    assert results
    index.close()
