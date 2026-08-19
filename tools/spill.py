import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from agent.token_counter import (
    PREVIEW_HEAD_TOKENS,
    PREVIEW_LIMIT_TOKENS,
    PREVIEW_TAIL_TOKENS,
    TokenCounter,
)
from memory.raw_store import RawConversationStore
from tools.base import ToolResult

SPILL_LIMIT_TOKENS = PREVIEW_LIMIT_TOKENS
SPILL_SKIP_TOOLS = frozenset({"read_file"})


def maybe_spill(
    result: ToolResult,
    *,
    tool_name: str,
    workspace: Path,
    session_id: str = "",
    counter: Optional[TokenCounter] = None,
) -> ToolResult:
    if not result.ok or getattr(result, "waiting_approval", False):
        return result
    if tool_name in SPILL_SKIP_TOOLS:
        return result
    content = result.content or ""
    if not content:
        return result
    counter = counter or TokenCounter()
    try:
        token_count = counter.count_text(content)
    except Exception:
        return result
    if token_count <= SPILL_LIMIT_TOKENS:
        return result

    rel_path, write_error = write_spill_file(
        content,
        workspace=workspace,
        session_id=session_id,
        tool_name=tool_name,
    )
    if not rel_path:
        return ToolResult(
            ok=True,
            content=format_truncated_preview(counter, content, token_count, write_error),
            waiting_approval=result.waiting_approval,
            approval_id=result.approval_id,
        )
    return ToolResult(
        ok=True,
        content=format_spilled_preview(counter, content, token_count, rel_path),
        waiting_approval=result.waiting_approval,
        approval_id=result.approval_id,
    )


def write_spill_file(
    content: str,
    *,
    workspace: Path,
    session_id: str,
    tool_name: str,
) -> tuple[str, str]:
    safe_session = RawConversationStore._safe_session_id(session_id)
    safe_tool = "".join(
        ch if ch.isalnum() or ch in {"-", "_"} else "_"
        for ch in (tool_name or "tool")
    ) or "tool"
    filename = (
        f"{datetime.now().strftime('%Y%m%dT%H%M%S')}_{safe_tool}_{uuid.uuid4().hex[:8]}.txt"
    )
    workspace = Path(workspace).resolve()
    path = workspace / ".agent" / "tool_outputs" / safe_session / filename
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    except Exception as error:
        return "", str(error)
    return path.relative_to(workspace).as_posix(), ""


def format_spilled_preview(
    counter: TokenCounter,
    content: str,
    token_count: int,
    rel_path: str,
) -> str:
    head, tail = _head_tail(counter, content)
    byte_count = len(content.encode("utf-8"))
    line_count = len(content.splitlines()) if content else 0
    return (
        f"{head}\n"
        f"\n"
        f"---\n"
        f"[spilled: {token_count} tokens, {byte_count} bytes, {line_count} lines]\n"
        f"Full output: {rel_path}\n"
        f"Use read_file with offset/limit to continue reading this file.\n"
        f"---\n"
        f"\n"
        f"{tail}"
    )


def format_truncated_preview(
    counter: TokenCounter,
    content: str,
    token_count: int,
    write_error: str = "",
) -> str:
    head, tail = _head_tail(counter, content)
    extra = f"; spill failed: {write_error}" if write_error else ""
    return (
        f"{head}\n\n"
        f"[truncated: {token_count} tokens{extra}; "
        f"showing head {PREVIEW_HEAD_TOKENS} + tail {PREVIEW_TAIL_TOKENS}]\n\n"
        f"{tail}"
    )


def _head_tail(counter: TokenCounter, text: str) -> tuple[str, str]:
    encoding = counter.encoding()
    token_ids = encoding.encode(text or "", disallowed_special=())
    head = encoding.decode(token_ids[:PREVIEW_HEAD_TOKENS])
    tail = encoding.decode(token_ids[-PREVIEW_TAIL_TOKENS:])
    return head, tail
