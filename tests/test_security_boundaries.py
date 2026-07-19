from pathlib import Path

import pytest

from tools.bash_tool import BashTool
from tools.file_tools import ReadFileTool, WriteFileTool

pytestmark = pytest.mark.security


def test_read_file_blocks_symlink_escape(tmp_workspace: Path, tmp_path: Path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = tmp_workspace / "link.txt"
    link.symlink_to(outside)

    result = ReadFileTool(tmp_workspace).run({"path": "link.txt"})

    assert result.ok is False
    assert "escape" in result.content.lower()


def test_write_file_blocks_symlink_escape(tmp_workspace: Path, tmp_path: Path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = tmp_workspace / "link.txt"
    link.symlink_to(outside)

    result = WriteFileTool(tmp_workspace).run({"path": "link.txt", "content": "modified"})

    assert result.ok is False
    assert "escape" in result.content.lower()
    assert outside.read_text(encoding="utf-8") == "secret"


def test_bash_blocks_semicolon_path_escape(tmp_workspace: Path):
    result = BashTool(tmp_workspace).run({"command": "echo ok; cat ../secret"})
    assert result.ok is False
    assert "traversal" in result.content.lower()


def test_bash_blocks_command_substitution_secret(tmp_workspace: Path):
    result = BashTool(tmp_workspace).run({"command": "echo $(cat .env)"})
    assert result.ok is False
    assert "credential" in result.content.lower()


def test_bash_blocks_launchctl_word_boundary(tmp_workspace: Path):
    result = BashTool(tmp_workspace).run({"command": "launchctl list"})
    assert result.ok is False
    assert "privileged" in result.content.lower()
