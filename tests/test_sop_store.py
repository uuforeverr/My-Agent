from pathlib import Path

from memory.sop_store import SopStore


def test_first_read_returns_skeleton(tmp_memory_dir: Path):
    store = SopStore(tmp_memory_dir)
    content = store.read()
    assert "Standard Operating Procedures" in content
    assert store.path.exists()


def test_overwrite_replaces_content(tmp_memory_dir: Path):
    store = SopStore(tmp_memory_dir)
    store.overwrite("## Deploy\n1. build\n2. push")
    content = store.read()
    assert "## Deploy" in content
    assert "1. build" in content
    assert "Standard Operating Procedures" not in content


def test_overwrite_empty_falls_back_to_skeleton(tmp_memory_dir: Path):
    store = SopStore(tmp_memory_dir)
    store.overwrite("real sop")
    store.overwrite("   ")
    assert "Standard Operating Procedures" in store.read()


def test_append_log_writes_timestamped_file(tmp_memory_dir: Path):
    store = SopStore(tmp_memory_dir)
    path = store.append_log("## Flow\n1. do x", "- you repeat x, script it", {"lookback_days": 7})
    assert path.exists()
    assert path.parent == tmp_memory_dir / "workflows" / "log"
    text = path.read_text(encoding="utf-8")
    assert "## SOP" in text
    assert "1. do x" in text
    assert "## Insights" in text
    assert "script it" in text
    assert "lookback_days: 7" in text


def test_append_log_handles_empty_insights(tmp_memory_dir: Path):
    store = SopStore(tmp_memory_dir)
    path = store.append_log("## Flow\n1. do x", "", None)
    text = path.read_text(encoding="utf-8")
    assert "(none)" in text
