import difflib
from pathlib import Path
from typing import Any, Optional

from tools.base import BaseTool, ToolError, ToolResult

L1_DIFF_PREVIEW_LINES = 40


MAX_READ_CHARS = 60000
MAX_GLOB_RESULTS = 200


class GlobTool(BaseTool):
    name = "glob"
    description = "Find files inside the workspace by glob pattern, such as '**/*.py'."
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Glob pattern relative to the workspace.",
            },
            "max_results": {
                "type": "integer",
                "description": f"Maximum number of paths to return. Default is {MAX_GLOB_RESULTS}.",
            },
        },
        "required": ["pattern"],
    }
    risk_level = "L0"
    idempotent = True

    def execute(self, arguments: dict[str, Any]) -> str:
        pattern = str(arguments.get("pattern", "")).strip()
        max_results = int(arguments.get("max_results", MAX_GLOB_RESULTS))
        self._validate_pattern(pattern)

        matches = []
        for path in sorted(self.workspace.glob(pattern)):
            resolved = path.resolve()
            if resolved != self.workspace and self.workspace not in resolved.parents:
                continue
            matches.append(resolved)
            if len(matches) >= max_results:
                break

        if not matches:
            return "(no matches)"

        lines = []
        for path in matches:
            suffix = "/" if path.is_dir() else ""
            lines.append(f"{path.relative_to(self.workspace)}{suffix}")
        return "\n".join(lines)

    def _validate_pattern(self, pattern: str) -> None:
        if not pattern:
            raise ToolError("pattern is required")
        if pattern.startswith("/") or pattern.startswith("~"):
            raise ToolError("pattern must be relative to the workspace")
        if ".." in Path(pattern).parts:
            raise ToolError("pattern must not contain '..'")


class ReadFileTool(BaseTool):
    name = "read_file"
    description = "Read a UTF-8 text file inside the workspace."
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File path relative to the workspace.",
            },
            "offset": {
                "type": "integer",
                "description": "1-based line number to start from. Use negative values to read from the end.",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of lines to read.",
            },
        },
        "required": ["path"],
    }
    risk_level = "L0"
    idempotent = True

    def run(self, arguments: dict[str, Any], context: Any = None) -> ToolResult:
        target = self._skill_activation_target(arguments)
        trace = getattr(context, "trace", None)
        if target is None or trace is None:
            return super().run(arguments, context)

        skill_name, rel_path = target
        with trace.start_span(
            "skill.activate",
            span_kind="skill.activate",
            session_id=getattr(context, "session_id", ""),
            skill_name=skill_name,
            file_path=str(rel_path),
            activation_source=self.name,
        ) as span:
            result = super().run(arguments, context)
            span.set_attributes(
                ok=result.ok,
                result_chars=len(result.content or ""),
            )
            if not result.ok:
                span.set_status("error")
            return result

    def execute(self, arguments: dict[str, Any]) -> str:
        path = self.resolve_workspace_path(arguments.get("path", ""))
        if not path.exists():
            raise ToolError(f"file not found: {arguments.get('path')}")
        if not path.is_file():
            raise ToolError(f"not a file: {arguments.get('path')}")
        content = path.read_text(encoding="utf-8")
        lines = content.splitlines()
        offset = arguments.get("offset")
        limit = arguments.get("limit")
        selected, start_line, end_line = _select_lines(lines, offset, limit)
        body = "\n".join(selected)
        body, truncated = _truncate_text(body, MAX_READ_CHARS)
        rel = path.relative_to(self.workspace)

        header = [
            f"File: {rel}",
            f"Lines: {start_line}-{end_line} of {len(lines)}",
        ]
        if truncated:
            header.append(f"Output truncated to {MAX_READ_CHARS} characters.")
        return "\n".join(header) + "\n\n" + body

    def _skill_activation_target(self, arguments: Any) -> Optional[tuple[str, Path]]:
        if not isinstance(arguments, dict):
            return None
        try:
            path = self.resolve_workspace_path(arguments.get("path", ""))
            rel_path = path.relative_to(self.workspace)
        except (ToolError, ValueError):
            return None

        parts = rel_path.parts
        if len(parts) == 3 and parts[0] == "skills" and parts[2] == "SKILL.md":
            return parts[1], rel_path
        return None


class WriteFileTool(BaseTool):
    name = "write_file"
    description = "Write UTF-8 text to a file inside the workspace, creating parent directories if needed."
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File path relative to the workspace.",
            },
            "content": {
                "type": "string",
                "description": "Text content to write.",
            },
        },
        "required": ["path", "content"],
    }
    risk_level = "L1"
    idempotent = False

    def execute(self, arguments: dict[str, Any]) -> str:
        path = self.resolve_workspace_path(arguments.get("path", ""))
        content = arguments.get("content")
        if content is None:
            raise ToolError("content is required")
        path.parent.mkdir(parents=True, exist_ok=True)
        text = str(content)
        path.write_text(text, encoding="utf-8")
        return f"written: {path.relative_to(self.workspace)} ({len(text.encode('utf-8'))} bytes)"

    def trace_preview(self, arguments: dict[str, Any]) -> dict[str, Any]:
        raw_path = arguments.get("path", "")
        try:
            path = self.resolve_workspace_path(str(raw_path or ""))
            rel = path.relative_to(self.workspace).as_posix()
            exists = path.exists()
        except Exception as e:
            return {"path": str(raw_path), "error": str(e)}
        text = str(arguments.get("content") or "")
        return {
            "path": rel,
            "bytes": len(text.encode("utf-8")),
            "new_file": not exists,
        }


class EditFileTool(BaseTool):
    name = "edit_file"
    description = "Replace text in a UTF-8 file exactly once, or append when old_text is empty."
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File path relative to the workspace.",
            },
            "old_text": {
                "type": "string",
                "description": "Text to replace. Must occur exactly once. Use empty string to append.",
            },
            "new_text": {
                "type": "string",
                "description": "Replacement text or appended text.",
            },
        },
        "required": ["path", "old_text", "new_text"],
    }
    risk_level = "L1"
    idempotent = False

    def execute(self, arguments: dict[str, Any]) -> str:
        path = self.resolve_workspace_path(arguments.get("path", ""))
        old_text = arguments.get("old_text", arguments.get("oldText"))
        new_text = arguments.get("new_text", arguments.get("newText"))

        if old_text is None:
            raise ToolError("old_text is required")
        if new_text is None:
            raise ToolError("new_text is required")
        if not path.exists():
            raise ToolError(f"file not found: {arguments.get('path')}")
        if not path.is_file():
            raise ToolError(f"not a file: {arguments.get('path')}")

        content = path.read_text(encoding="utf-8")
        old = str(old_text)
        new = str(new_text)

        if old == "":
            updated = _append_text(content, new)
            first_changed_line = len(content.splitlines()) + 1
        else:
            count = content.count(old)
            if count == 0:
                raise ToolError("old_text was not found")
            if count > 1:
                raise ToolError(f"old_text occurs {count} times; provide more context")
            first_changed_line = content[:content.index(old)].count("\n") + 1
            updated = content.replace(old, new, 1)

        if updated == content:
            raise ToolError("edit produced no change")

        path.write_text(updated, encoding="utf-8")
        rel = path.relative_to(self.workspace)
        return f"edited: {rel} (first_changed_line={first_changed_line})"

    def trace_preview(self, arguments: dict[str, Any]) -> dict[str, Any]:
        raw_path = arguments.get("path", "")
        try:
            path = self.resolve_workspace_path(str(raw_path or ""))
            rel = path.relative_to(self.workspace).as_posix()
        except Exception as e:
            return {"path": str(raw_path), "error": str(e)}
        if not path.exists() or not path.is_file():
            return {"path": rel, "error": "file not found"}
        old_text = arguments.get("old_text", arguments.get("oldText"))
        new_text = arguments.get("new_text", arguments.get("newText"))
        if old_text is None or new_text is None:
            return {"path": rel, "error": "missing old_text/new_text"}
        try:
            content = path.read_text(encoding="utf-8")
            old = str(old_text)
            new = str(new_text)
            if old == "":
                updated = _append_text(content, new)
            else:
                if content.count(old) != 1:
                    return {"path": rel, "error": "old_text count mismatch"}
                updated = content.replace(old, new, 1)
        except Exception as e:
            return {"path": rel, "error": str(e)}
        diff_lines = list(
            difflib.unified_diff(
                content.splitlines(),
                updated.splitlines(),
                fromfile=rel,
                tofile=rel,
                lineterm="",
            )
        )
        return {
            "path": rel,
            "diff_preview": "\n".join(diff_lines[:L1_DIFF_PREVIEW_LINES]),
            "diff_lines": len(diff_lines),
        }


def _select_lines(
    lines: list[str],
    offset: Any,
    limit: Any,
) -> tuple[list[str], int, int]:
    total = len(lines)
    if total == 0:
        return [], 1, 0

    if offset is None:
        start_index = 0
    else:
        raw_offset = int(offset)
        if raw_offset < 0:
            start_index = max(total + raw_offset, 0)
        else:
            start_index = max(raw_offset - 1, 0)

    if limit is None:
        end_index = total
    else:
        end_index = min(start_index + max(int(limit), 0), total)

    selected = lines[start_index:end_index]
    start_line = start_index + 1
    end_line = start_index + len(selected)
    return selected, start_line, end_line


def _truncate_text(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars], True


def _append_text(content: str, new_text: str) -> str:
    if not content:
        return new_text
    if content.endswith("\n"):
        return content + new_text
    return content + "\n" + new_text
