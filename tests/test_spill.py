from pathlib import Path

import pytest

from agent.run_context import RunContext
from agent.token_counter import TokenCounter
from memory.manager import MemoryManager
from tools.base import BaseTool, ToolResult
from tools.registry import ToolRegistry

MARKER = "UNIQUE_MIDDLE_MARKER_XYZ"


class _FixedOutputTool(BaseTool):
    name = "bash"
    risk_level = "L2"
    idempotent = False

    def __init__(self, workspace: Path, content: str, ok: bool = True):
        super().__init__(workspace)
        self._content = content
        self._ok = ok

    def run(self, arguments, context=None):
        return ToolResult(ok=self._ok, content=self._content)

    def execute(self, arguments):
        return self._content


@pytest.fixture
def registry(tmp_workspace, tmp_memory_dir):
    memory = MemoryManager(tmp_memory_dir, llm=None)
    try:
        yield ToolRegistry(tmp_workspace, memory)
    finally:
        memory.close()


def _text_with_tokens(n: int, marker: str = "") -> str:
    counter = TokenCounter()
    if marker:
        head = ""
        while counter.count_text(head) < 900:
            head += "word "
        text = head + marker
        while counter.count_text(text) < n:
            text += "word "
    else:
        text = ""
        i = 0
        while True:
            piece = f"w{i} "
            nxt = text + piece
            if counter.count_text(nxt) > n:
                break
            text = nxt
            i += 1
        for ch in "abcdefghijklmnopqrstuvwxyz0123456789":
            while counter.count_text(text + ch) <= n:
                text += ch
            if counter.count_text(text) == n:
                break
    assert counter.count_text(text) >= n
    if n <= 2000:
        assert counter.count_text(text) == n
    if marker:
        assert marker in text
    return text


def _spill_files(workspace: Path) -> list[Path]:
    root = Path(workspace) / ".agent" / "tool_outputs"
    if not root.exists():
        return []
    return [path for path in root.rglob("*.txt") if path.is_file()]


def test_spill_over_limit_writes_file_and_preview(registry, tmp_workspace):
    huge = _text_with_tokens(2010, MARKER)
    token_count = TokenCounter().count_text(huge)
    registry.tools["bash"] = _FixedOutputTool(tmp_workspace, huge)
    context = RunContext.create("cli:local", tmp_workspace)

    result = registry.run("bash", {"command": "ignored"}, context)

    assert result.ok
    assert token_count > 2000
    assert "[spilled:" in result.content
    assert f"{token_count} tokens" in result.content
    assert "Full output:" in result.content
    assert "read_file" in result.content
    assert "offset/limit" in result.content
    assert MARKER not in result.content

    files = _spill_files(tmp_workspace)
    assert len(files) == 1
    assert files[0].read_text(encoding="utf-8") == huge
    rel = files[0].relative_to(Path(tmp_workspace).resolve()).as_posix()
    assert rel in result.content
    assert rel.startswith(".agent/tool_outputs/")

    read_back = registry.run("read_file", {"path": rel}, context)
    assert read_back.ok
    assert "File:" in read_back.content
    assert MARKER in read_back.content


def test_spill_under_limit_keeps_content(registry, tmp_workspace):
    text = _text_with_tokens(1990)
    registry.tools["bash"] = _FixedOutputTool(tmp_workspace, text)
    context = RunContext.create("cli:local", tmp_workspace)

    result = registry.run("bash", {"command": "ignored"}, context)

    assert result.ok
    assert result.content == text
    assert "[spilled:" not in result.content
    assert _spill_files(tmp_workspace) == []


def test_spill_write_failure_truncates(monkeypatch, registry, tmp_workspace):
    monkeypatch.setattr(
        "tools.spill.write_spill_file",
        lambda *args, **kwargs: ("", "disk full"),
    )
    huge = _text_with_tokens(2010, MARKER)
    registry.tools["bash"] = _FixedOutputTool(tmp_workspace, huge)
    context = RunContext.create("cli:local", tmp_workspace)

    result = registry.run("bash", {"command": "ignored"}, context)

    assert result.ok
    assert "[truncated:" in result.content
    assert "spill failed" in result.content
    assert "disk full" in result.content
    assert MARKER not in result.content
    assert _spill_files(tmp_workspace) == []


def test_read_file_does_not_spill(registry, tmp_workspace):
    text = _text_with_tokens(2010)
    assert len(text) < 60000
    (tmp_workspace / "big.txt").write_text(text, encoding="utf-8")
    context = RunContext.create("cli:local", tmp_workspace)

    result = registry.run("read_file", {"path": "big.txt"}, context)

    assert result.ok
    assert "File:" in result.content
    assert "Lines:" in result.content
    assert "[spilled:" not in result.content
    assert _spill_files(tmp_workspace) == []


def test_spill_session_id_with_colon_is_safe(registry, tmp_workspace):
    huge = _text_with_tokens(2010)
    registry.tools["bash"] = _FixedOutputTool(tmp_workspace, huge)
    context = RunContext.create("cli:alice", tmp_workspace)

    result = registry.run("bash", {"command": "ignored"}, context)

    assert result.ok
    session_dir = Path(tmp_workspace).resolve() / ".agent" / "tool_outputs" / "cli_alice"
    assert session_dir.is_dir()
    files = list(session_dir.glob("*.txt"))
    assert len(files) == 1
    assert ":" not in str(files[0].relative_to(session_dir.parent))
    assert "cli_alice" in result.content


def test_failed_tool_does_not_spill(registry, tmp_workspace):
    huge = _text_with_tokens(2010, MARKER)
    registry.tools["bash"] = _FixedOutputTool(tmp_workspace, huge, ok=False)
    context = RunContext.create("cli:local", tmp_workspace)

    result = registry.run("bash", {"command": "ignored"}, context)

    assert result.ok is False
    assert result.content == huge
    assert _spill_files(tmp_workspace) == []
