from pathlib import Path

import pytest

from tools.bash_tool import BashTool
from tools.file_tools import ReadFileTool, WriteFileTool
from tools.sandbox import SandboxResult

pytestmark = pytest.mark.security


def test_read_file_blocks_symlink_escape(tmp_workspace: Path, tmp_path: Path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = tmp_workspace / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError as error:
        pytest.skip(f"symlink creation is not permitted on this host: {error}")

    result = ReadFileTool(tmp_workspace).run({"path": "link.txt"})

    assert result.ok is False
    assert "escape" in result.content.lower()


def test_write_file_blocks_symlink_escape(tmp_workspace: Path, tmp_path: Path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = tmp_workspace / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError as error:
        pytest.skip(f"symlink creation is not permitted on this host: {error}")

    result = WriteFileTool(tmp_workspace).run({"path": "link.txt", "content": "modified"})

    assert result.ok is False
    assert "escape" in result.content.lower()
    assert outside.read_text(encoding="utf-8") == "secret"


class _Sandbox:
    def __init__(self):
        self.commands = []

    def execute(self, command, *, profile, timeout):
        self.commands.append(command)
        return SandboxResult(0, "ok", "")


def test_bash_shell_syntax_stays_inside_sandbox(tmp_workspace: Path):
    executor = _Sandbox()
    result = BashTool(tmp_workspace, executor=executor).run({"command": "echo ok; cat ../secret"})
    assert result.ok is True
    assert executor.commands == ["echo ok; cat ../secret"]


def test_bash_blocks_command_substitution_secret(tmp_workspace: Path):
    result = BashTool(tmp_workspace, executor=_Sandbox()).run({"command": "echo $(cat .env)"})
    assert result.ok is False
    assert "credential" in result.content.lower()


@pytest.mark.parametrize("command", ["launchctl list", "echo hello", "pytest | cat", "curl http://example.com"])
def test_bash_general_commands_are_delegated_to_locked_sandbox(tmp_workspace: Path, command: str):
    executor = _Sandbox()
    result = BashTool(tmp_workspace, executor=executor).run({"command": command})
    assert result.ok is True
    assert executor.commands == [command]
