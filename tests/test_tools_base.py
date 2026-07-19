from pathlib import Path

import pytest

from tools.base import BaseTool, ToolError, ToolResult


class _EchoTool(BaseTool):
    name = "echo"

    def execute(self, arguments):
        if arguments.get("boom"):
            raise ToolError("boom")
        return "echoed"


def test_resolve_workspace_path_normal(tmp_workspace: Path):
    tool = _EchoTool(tmp_workspace)
    resolved = tool.resolve_workspace_path("a/b.txt")
    assert resolved == (tmp_workspace / "a" / "b.txt").resolve()


def test_resolve_workspace_path_empty(tmp_workspace: Path):
    tool = _EchoTool(tmp_workspace)
    with pytest.raises(ToolError):
        tool.resolve_workspace_path("")


def test_resolve_workspace_path_parent_escape(tmp_workspace: Path):
    tool = _EchoTool(tmp_workspace)
    with pytest.raises(ToolError):
        tool.resolve_workspace_path("../secret.txt")


def test_resolve_workspace_path_absolute_escape(tmp_workspace: Path):
    tool = _EchoTool(tmp_workspace)
    with pytest.raises(ToolError):
        tool.resolve_workspace_path("/etc/passwd")


def test_run_wraps_success(tmp_workspace: Path):
    tool = _EchoTool(tmp_workspace)
    result = tool.run({})
    assert isinstance(result, ToolResult)
    assert result.ok is True
    assert result.content == "echoed"


def test_run_wraps_error(tmp_workspace: Path):
    tool = _EchoTool(tmp_workspace)
    result = tool.run({"boom": True})
    assert result.ok is False
    assert "ToolError" in result.content
    assert "boom" in result.content


def test_schema_shape(tmp_workspace: Path):
    tool = _EchoTool(tmp_workspace)
    schema = tool.schema()
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "echo"
    assert "parameters" in schema["function"]
