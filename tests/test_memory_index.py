from pathlib import Path

from memory.embeddings import EmbeddingStatus
from memory.index import MemoryIndex


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class _FakeEmbedder:
    model_id = "fake-local"
    dimensions = 2
    available = True
    error = ""

    def warmup(self):
        return EmbeddingStatus("fake", self.model_id, True, self.dimensions)

    def encode(self, texts):
        vectors = []
        for text in texts:
            normalized = str(text)
            if any(term in normalized for term in ("审批超时", "无人确认", "没人确认")):
                vectors.append([1.0, 0.0])
            else:
                vectors.append([0.0, 1.0])
        return vectors


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


def test_hybrid_search_filters_user_before_bm25_and_vector(tmp_path: Path):
    root = tmp_path / "memory"
    alice = root / "daily" / "alice.md"
    bob = root / "daily" / "bob.md"
    _write(alice, "## A\n\nuser_id: `alice`\n\n- 审批超时后进入等待")
    _write(bob, "## B\n\nuser_id: `bob`\n\n- 审批超时后直接放行")
    index = MemoryIndex(
        root / "index.db",
        embedder=_FakeEmbedder(),
        vector_backend="python",
    )
    index.rebuild([(alice, "episodic"), (bob, "episodic")], root=root)

    alice_results = index.search("没人确认怎么办", user_id="alice", limit=5)
    bob_results = index.search("没人确认怎么办", user_id="bob", limit=5)

    assert alice_results and {result.record.user_id for result in alice_results} == {"alice"}
    assert bob_results and {result.record.user_id for result in bob_results} == {"bob"}
    assert "进入等待" in alice_results[0].record.content
    assert "直接放行" in bob_results[0].record.content
    assert alice_results[0].vector_rank == 1
    index.close()


def test_real_bm25_rank_is_fused_with_vector_rank(tmp_path: Path):
    root = tmp_path / "memory"
    daily = root / "daily" / "2026-10-14.md"
    _write(
        daily,
        "## Exact\n\nuser_id: `alice`\n\n- 调度审批超时处理\n\n"
        "## Semantic\n\nuser_id: `alice`\n\n- 无人确认时暂停执行",
    )
    index = MemoryIndex(
        root / "index.db",
        embedder=_FakeEmbedder(),
        vector_backend="python",
        rrf_k=60,
    )
    index.rebuild([(daily, "episodic")], root=root)

    results, metadata = index.search_with_metadata(
        "审批超时",
        user_id="alice",
        limit=5,
    )

    assert results
    assert results[0].bm25_rank == 1
    assert results[0].vector_rank is not None
    assert metadata["bm25_candidates"] >= 1
    assert metadata["vector_backend"] == "python"
    index.close()
