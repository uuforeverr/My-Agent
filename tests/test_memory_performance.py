import time

import pytest

from memory.index import MemoryIndex

pytestmark = pytest.mark.performance


def test_memory_index_rebuild_and_search_100_files_5000_chunks(tmp_path):
    root = tmp_path / "memory"
    daily_dir = root / "daily"
    daily_dir.mkdir(parents=True)

    files = []
    for file_index in range(100):
        path = daily_dir / f"2026-07-{file_index + 1:02d}.md"
        parts = [f"# Daily Memory: 2026-07-{file_index + 1:02d}"]
        for chunk_index in range(50):
            parts.append(
                f"## Event {chunk_index}\n\n"
                f"- performance_token_{file_index}_{chunk_index} "
                f"用户完成了第 {file_index}-{chunk_index} 个性能测试事件。"
            )
        path.write_text("\n\n".join(parts), encoding="utf-8")
        files.append((path, "episodic"))

    index = MemoryIndex(root / "index.db")
    start = time.perf_counter()
    index.rebuild(files, root=root)
    rebuild_seconds = time.perf_counter() - start

    start = time.perf_counter()
    results = index.search("performance_token_99_49", limit=5)
    search_seconds = time.perf_counter() - start

    assert results
    assert any("performance_token_99_49" in r.record.content for r in results)
    # 宽松阈值：本用例是退化哨兵，不是严格性能基准。
    assert rebuild_seconds < 8.0
    assert search_seconds < 0.5
    index.close()
