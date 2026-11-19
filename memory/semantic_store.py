from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
from threading import RLock
from typing import Any, Optional


LEGACY_MEMORY_ID_PATTERN = r"M-[A-F0-9]{10}"
TIMESTAMP_MEMORY_ID_PATTERN = r"M-\d{8}T\d{9}-\d{3,}"
MEMORY_ID_PATTERN = rf"(?:{LEGACY_MEMORY_ID_PATTERN}|{TIMESTAMP_MEMORY_ID_PATTERN})"
_VISIBLE_ID_RE = re.compile(rf"^\s*-\s*\[({MEMORY_ID_PATTERN})\]\s+", re.IGNORECASE)


class SemanticMemoryError(ValueError):
    def __init__(self, message: str, *, code: str, details: Optional[dict[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


class SemanticMemoryStore:
    """One human-readable MEMORY.md with stable, visible entry identifiers."""

    def __init__(self, memory_dir: Path):
        self.memory_dir = memory_dir
        self.path = memory_dir / "MEMORY.md"
        self.user_dir = memory_dir / "semantic"
        self._lock = RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.user_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_exists(self.path)
        self._merge_legacy_channel_memories()

    def read(self, user_id: str = "local") -> str:
        with self._lock:
            path = self.path_for(user_id)
            self._ensure_exists(path)
            self._ensure_stable_ids(path)
            return path.read_text(encoding="utf-8").strip()

    def remember(self, content: str, source: str = "manual", user_id: str = "local") -> str:
        try:
            result = self.apply_operation(
                {"op": "ADD", "content": content},
                user_id=user_id,
                source=source,
            )
        except SemanticMemoryError as exc:
            if exc.code == "duplicate_memory":
                return f"Memory already exists: {exc.details.get('memory_id', '')}".rstrip()
            if exc.code == "empty_content":
                return "No memory saved: content is empty."
            raise
        return f"Semantic memory saved: {result['memory_id']}"

    def apply_operation(
        self,
        operation: dict[str, Any],
        *,
        user_id: str = "local",
        source: str = "manual",
    ) -> dict[str, str]:
        op = str(operation.get("op") or "").strip().upper()
        content = str(operation.get("content") or "").strip()
        memory_id = str(operation.get("id") or operation.get("memory_id") or "").strip().upper()
        with self._lock:
            self._apply_operations_locked([{
                "op": op,
                "id": memory_id,
                "content": content,
            }], user_id=user_id, source=source)
            if op == "UPDATE":
                result_id = memory_id
            else:
                lines = self.path_for(user_id).read_text(encoding="utf-8").splitlines()
                result_id = self._find_content_id(lines, content)
        return {"operation": op, "memory_id": result_id, "content": content}

    def apply_operations(
        self,
        operations: list[dict[str, Any]],
        *,
        user_id: str = "local",
        source: str = "dream",
    ) -> dict[str, int]:
        with self._lock:
            return self._apply_operations_locked(
                operations,
                user_id=user_id,
                source=source,
            )

    def _apply_operations_locked(
        self,
        operations: list[dict[str, Any]],
        *,
        user_id: str,
        source: str,
    ) -> dict[str, int]:
        if not isinstance(operations, list) or not operations:
            raise SemanticMemoryError(
                "operations must be a non-empty array",
                code="invalid_operations",
            )

        path = self.path_for(user_id)
        self._ensure_exists(path)
        self._ensure_stable_ids(path)
        lines = path.read_text(encoding="utf-8").splitlines()
        applied = {"ADD": 0, "UPDATE": 0, "SUPERSEDE": 0, "NOOP": 0}

        def find(memory_id: str) -> Optional[int]:
            normalized = memory_id.upper()
            return next(
                (
                    index
                    for index, line in enumerate(lines)
                    if self._line_memory_id(line) == normalized
                ),
                None,
            )

        for position, operation in enumerate(operations):
            if not isinstance(operation, dict):
                raise SemanticMemoryError(
                    f"operation[{position}] must be an object",
                    code="invalid_operation",
                    details={"position": position},
                )
            op = str(operation.get("op") or "").strip().upper()
            if op not in applied:
                raise SemanticMemoryError(
                    f"operation[{position}].op must be ADD, UPDATE, SUPERSEDE, or NOOP; got {op!r}",
                    code="invalid_operation",
                    details={"position": position, "received_op": op},
                )
            if op == "NOOP":
                applied[op] += 1
                continue

            content = str(operation.get("content") or "").strip()
            if not content:
                raise SemanticMemoryError(
                    f"operation[{position}].content is required for {op}",
                    code="empty_content",
                    details={"position": position, "operation": op},
                )

            target_id = str(operation.get("id") or operation.get("memory_id") or "").strip().upper()
            if op == "ADD":
                if target_id:
                    raise SemanticMemoryError(
                        f"operation[{position}].memory_id is generated by the store and must not be provided",
                        code="generated_field_not_allowed",
                        details={"position": position, "field": "memory_id"},
                    )
                existing_id = self._find_content_id(lines, content)
                if existing_id:
                    raise SemanticMemoryError(
                        f"memory already exists as {existing_id}",
                        code="duplicate_memory",
                        details={"position": position, "memory_id": existing_id},
                    )
                memory_id = self._new_memory_id(lines)
                section = "## Dream Updates" if source == "dream" else "## Realtime Updates"
                if section not in lines:
                    lines.extend(["", section, ""])
                lines.append(self._format_line(memory_id, content))
                applied[op] += 1
                continue

            self._validate_memory_id(target_id, position)
            index = find(target_id)
            if index is None:
                raise SemanticMemoryError(
                    f"memory id not found: {target_id}",
                    code="memory_not_found",
                    details={
                        "position": position,
                        "memory_id": target_id,
                        "available_memory_ids": self._active_ids(lines),
                    },
                )

            if op == "UPDATE":
                lines[index] = self._format_line(target_id, content)
            else:
                duplicate_id = self._find_content_id(lines, content)
                if duplicate_id and duplicate_id != target_id:
                    raise SemanticMemoryError(
                        f"content already belongs to {duplicate_id}",
                        code="duplicate_memory",
                        details={
                            "position": position,
                            "memory_id": duplicate_id,
                            "target_memory_id": target_id,
                        },
                    )
                if "new_id" in operation:
                    raise SemanticMemoryError(
                        f"operation[{position}].new_id is generated by the store and must not be provided",
                        code="generated_field_not_allowed",
                        details={"position": position, "field": "new_id"},
                    )
                new_id = self._new_memory_id(lines)
                lines.pop(index)
                lines.append(self._format_line(new_id, content))
            applied[op] += 1

        path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        return applied

    def overwrite(self, content: str, user_id: str = "local") -> None:
        with self._lock:
            text = content.strip() or "# Semantic Memory\n\n"
            path = self.path_for(user_id)
            self._ensure_exists(path)
            path.write_text(text.rstrip() + "\n", encoding="utf-8")

    def path_for(self, user_id: str = "local") -> Path:
        return self.path

    def _ensure_exists(self, path: Path) -> None:
        if path.exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "# Semantic Memory\n\n"
            "每条活跃记忆使用可见稳定编号 `[M-YYYYMMDDTHHMMSSmmm-NNN]`，供工具精确定位。\n\n"
            "## User Preferences\n\n"
            "## Project Decisions\n",
            encoding="utf-8",
        )

    def _ensure_manual_section(self, path: Path) -> None:
        content = path.read_text(encoding="utf-8").strip()
        if "## Manual Notes" in content:
            return
        with path.open("a", encoding="utf-8") as file:
            file.write("\n\n## Manual Notes\n")

    def _merge_legacy_channel_memories(self) -> None:
        for legacy_path in sorted(self.user_dir.glob("*.md")):
            for line in legacy_path.read_text(encoding="utf-8").splitlines():
                if not line.lstrip().startswith("- "):
                    continue
                content = self._line_content(line)
                if content:
                    self.remember(content, source="legacy-channel-memory")

    def _ensure_stable_ids(self, path: Path) -> None:
        lines = path.read_text(encoding="utf-8").splitlines()
        changed = False
        for index, line in enumerate(lines):
            without_debug_comment = re.sub(r"\s*<!--.*?-->\s*$", "", line).rstrip()
            if without_debug_comment != line:
                line = without_debug_comment
                lines[index] = line
                changed = True
            if not line.lstrip().startswith("- "):
                continue
            content = self._line_content(line)
            if not content:
                continue
            memory_id = self._line_memory_id(line) or self._new_memory_id(lines)
            normalized = f"- [{memory_id}] {content}"
            if lines[index] != normalized:
                lines[index] = normalized
                changed = True
        if changed:
            path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

    @classmethod
    def _find_content_id(cls, lines: list[str], content: str) -> str:
        needle = content.strip().casefold()
        for line in lines:
            memory_id = cls._line_memory_id(line)
            if memory_id and cls._line_content(line).casefold() == needle:
                return memory_id
        return ""

    @classmethod
    def _active_ids(cls, lines: list[str]) -> list[str]:
        return [memory_id for line in lines if (memory_id := cls._line_memory_id(line))]

    @staticmethod
    def _line_memory_id(line: str) -> str:
        match = _VISIBLE_ID_RE.match(line)
        return match.group(1).upper() if match else ""

    @staticmethod
    def _line_content(line: str) -> str:
        cleaned = line.strip().split("<!--", 1)[0].strip()
        cleaned = cleaned[1:].strip() if cleaned.startswith("-") else cleaned
        cleaned = re.sub(rf"^\[{MEMORY_ID_PATTERN}\]\s*", "", cleaned, flags=re.IGNORECASE)
        return cleaned.strip()

    @classmethod
    def _new_memory_id(
        cls,
        lines: list[str],
        *,
        now: Optional[datetime] = None,
    ) -> str:
        wall_time = now or datetime.now().astimezone()
        prefix = (
            "M-"
            + wall_time.strftime("%Y%m%dT%H%M%S")
            + f"{wall_time.microsecond // 1000:03d}-"
        )
        existing_ids = set(cls._active_ids(lines))
        sequence = 0
        while True:
            candidate = f"{prefix}{sequence:03d}"
            if candidate not in existing_ids:
                return candidate
            sequence += 1

    @staticmethod
    def _validate_memory_id(
        memory_id: str,
        position: int,
        *,
        field: str = "memory_id",
    ) -> None:
        if not re.fullmatch(MEMORY_ID_PATTERN, memory_id or "", flags=re.IGNORECASE):
            raise SemanticMemoryError(
                f"operation[{position}].{field} must match {MEMORY_ID_PATTERN}; got {memory_id!r}",
                code="invalid_memory_id",
                details={"position": position, "field": field, "received": memory_id},
            )

    @staticmethod
    def _format_line(memory_id: str, content: str) -> str:
        return f"- [{memory_id.upper()}] {content.strip()}"
