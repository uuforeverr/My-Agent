from pathlib import Path

from tools.file_tools import EditFileTool, GlobTool, ReadFileTool, WriteFileTool


def test_write_file_creates_parent_and_reports_bytes(tmp_workspace: Path):
    tool = WriteFileTool(tmp_workspace)
    result = tool.run({"path": "sub/a.txt", "content": "hello"})
    assert result.ok
    assert "written" in result.content
    assert (tmp_workspace / "sub" / "a.txt").read_text(encoding="utf-8") == "hello"


def test_write_file_rejects_escape(tmp_workspace: Path):
    tool = WriteFileTool(tmp_workspace)
    result = tool.run({"path": "../evil.txt", "content": "x"})
    assert result.ok is False
    assert "escape" in result.content.lower()


def test_read_file_basic(tmp_workspace: Path):
    (tmp_workspace / "a.txt").write_text("l1\nl2\nl3\n", encoding="utf-8")
    tool = ReadFileTool(tmp_workspace)
    result = tool.run({"path": "a.txt"})
    assert result.ok
    assert "l1" in result.content and "l3" in result.content
    assert "Lines: 1-3 of 3" in result.content


def test_read_file_offset_limit(tmp_workspace: Path):
    (tmp_workspace / "a.txt").write_text("l1\nl2\nl3\nl4\n", encoding="utf-8")
    tool = ReadFileTool(tmp_workspace)
    result = tool.run({"path": "a.txt", "offset": 2, "limit": 2})
    assert result.ok
    body = result.content.split("\n\n", 1)[1]
    assert body == "l2\nl3"


def test_read_file_negative_offset(tmp_workspace: Path):
    (tmp_workspace / "a.txt").write_text("l1\nl2\nl3\nl4\n", encoding="utf-8")
    tool = ReadFileTool(tmp_workspace)
    result = tool.run({"path": "a.txt", "offset": -2})
    assert result.ok
    body = result.content.split("\n\n", 1)[1]
    assert body == "l3\nl4"


def test_read_file_not_found(tmp_workspace: Path):
    tool = ReadFileTool(tmp_workspace)
    result = tool.run({"path": "missing.txt"})
    assert result.ok is False
    assert "not found" in result.content.lower()


def test_read_file_truncates_large_output(tmp_workspace: Path):
    (tmp_workspace / "large.txt").write_text("x" * 70000, encoding="utf-8")
    tool = ReadFileTool(tmp_workspace)
    result = tool.run({"path": "large.txt"})
    assert result.ok
    assert "Output truncated" in result.content


def test_edit_file_unique_replace(tmp_workspace: Path):
    (tmp_workspace / "a.txt").write_text("alpha beta gamma", encoding="utf-8")
    tool = EditFileTool(tmp_workspace)
    result = tool.run({"path": "a.txt", "old_text": "beta", "new_text": "BETA"})
    assert result.ok
    assert (tmp_workspace / "a.txt").read_text(encoding="utf-8") == "alpha BETA gamma"


def test_edit_file_zero_match(tmp_workspace: Path):
    (tmp_workspace / "a.txt").write_text("alpha", encoding="utf-8")
    tool = EditFileTool(tmp_workspace)
    result = tool.run({"path": "a.txt", "old_text": "zzz", "new_text": "x"})
    assert result.ok is False
    assert "not found" in result.content.lower()


def test_edit_file_multiple_match(tmp_workspace: Path):
    (tmp_workspace / "a.txt").write_text("x x x", encoding="utf-8")
    tool = EditFileTool(tmp_workspace)
    result = tool.run({"path": "a.txt", "old_text": "x", "new_text": "y"})
    assert result.ok is False
    assert "occurs" in result.content.lower()


def test_edit_file_append_on_empty_old(tmp_workspace: Path):
    (tmp_workspace / "a.txt").write_text("line1", encoding="utf-8")
    tool = EditFileTool(tmp_workspace)
    result = tool.run({"path": "a.txt", "old_text": "", "new_text": "line2"})
    assert result.ok
    assert (tmp_workspace / "a.txt").read_text(encoding="utf-8") == "line1\nline2"


def test_edit_file_alias_camelcase(tmp_workspace: Path):
    (tmp_workspace / "a.txt").write_text("foo", encoding="utf-8")
    tool = EditFileTool(tmp_workspace)
    result = tool.run({"path": "a.txt", "oldText": "foo", "newText": "bar"})
    assert result.ok
    assert (tmp_workspace / "a.txt").read_text(encoding="utf-8") == "bar"


def test_glob_matches_and_dir_suffix(tmp_workspace: Path):
    (tmp_workspace / "pkg").mkdir()
    (tmp_workspace / "pkg" / "m.py").write_text("x", encoding="utf-8")
    (tmp_workspace / "top.py").write_text("x", encoding="utf-8")
    tool = GlobTool(tmp_workspace)
    result = tool.run({"pattern": "**/*.py"})
    assert result.ok
    assert "pkg/m.py" in result.content
    assert "top.py" in result.content


def test_glob_no_match(tmp_workspace: Path):
    tool = GlobTool(tmp_workspace)
    result = tool.run({"pattern": "*.rs"})
    assert result.ok
    assert result.content == "(no matches)"


def test_glob_rejects_absolute_and_parent(tmp_workspace: Path):
    tool = GlobTool(tmp_workspace)
    assert tool.run({"pattern": "/etc/*"}).ok is False
    assert tool.run({"pattern": "../*"}).ok is False


def test_glob_max_results(tmp_workspace: Path):
    for i in range(5):
        (tmp_workspace / f"f{i}.txt").write_text("x", encoding="utf-8")
    tool = GlobTool(tmp_workspace)
    result = tool.run({"pattern": "*.txt", "max_results": 2})
    assert result.ok
    assert len(result.content.splitlines()) == 2
