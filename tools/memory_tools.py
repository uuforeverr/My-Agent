from pathlib import Path
from typing import Any

from memory import MemoryManager
from memory.semantic_store import MEMORY_ID_PATTERN, SemanticMemoryError
from tools.base import BaseTool, ToolResult


class RememberMemoryTool(BaseTool):
    name = "remember_memory"
    description = (
        "Create or update one durable semantic memory. Call ADD when a new stable fact, "
        "preference, constraint, or decision appears. Call UPDATE with the visible memory_id "
        "when the user corrects or contradicts an existing memory."
    )
    parameters = {
        "type": "object",
        "oneOf": [
            {
                "type": "object",
                "properties": {
                    "operation": {"const": "ADD"},
                    "content": {
                        "type": "string",
                        "minLength": 1,
                        "description": "The complete new durable memory content.",
                    },
                },
                "required": ["operation", "content"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "operation": {"const": "UPDATE"},
                    "memory_id": {
                        "type": "string",
                        "pattern": f"^{MEMORY_ID_PATTERN}$",
                        "description": "Complete visible target id copied from MEMORY.md.",
                    },
                    "content": {
                        "type": "string",
                        "minLength": 1,
                        "description": "The complete corrected durable memory content.",
                    },
                },
                "required": ["operation", "memory_id", "content"],
                "additionalProperties": False,
            },
        ],
    }
    risk_level = "R1"
    idempotent = False

    def __init__(self, workspace: Path, memory: MemoryManager):
        super().__init__(workspace)
        self.memory = memory

    def run(self, arguments: dict[str, Any], context: Any = None) -> ToolResult:
        try:
            result = self.memory.mutate_semantic_memory(
                str(arguments.get("operation") or ""),
                str(arguments.get("content") or ""),
                memory_id=str(arguments.get("memory_id") or ""),
                source=self.name,
            )
            return ToolResult(
                ok=True,
                content=(
                    f"{result['operation']} applied to {result['memory_id']}: "
                    f"{result['content']}"
                ),
                metadata={"memory_id": result["memory_id"], "operation": result["operation"]},
            )
        except SemanticMemoryError as exc:
            return ToolResult(
                ok=False,
                content=str(exc),
                error_code=exc.code,
                error_type="semantic_memory_validation_error",
                retryable=True,
                details=exc.details,
                suggested_action=(
                    "Read the visible memory ids, correct the function arguments, and call "
                    "remember_memory again"
                ),
            )
        except Exception as exc:
            return ToolResult(
                ok=False,
                content=f"{type(exc).__name__}: {exc}",
                error_code="memory_update_failed",
                error_type=type(exc).__name__,
                retryable=True,
                suggested_action="Inspect the error and retry with corrected arguments",
            )

    def execute(self, arguments: dict[str, Any]) -> str:
        result = self.run(arguments)
        if not result.ok:
            raise RuntimeError(result.content)
        return result.content


class SearchMemoryTool(BaseTool):
    name = "memory_search"
    description = "Search episodic memory by natural language query. Semantic memory is already loaded in the prompt."
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Natural language query or keywords to search in memory.",
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum number of memories to return. Default is 5.",
            },
        },
        "required": ["query"],
    }
    risk_level = "R0"
    idempotent = True

    def __init__(self, workspace: Path, memory: MemoryManager):
        super().__init__(workspace)
        self.memory = memory

    def run(self, arguments: dict[str, Any], context: Any = None) -> ToolResult:
        try:
            content = self._search_content(
                arguments,
                user_id="local",
                trace=getattr(context, "trace", None),
            )
            return ToolResult(ok=True, content=content)
        except Exception as exc:
            return ToolResult(ok=False, content=f"{type(exc).__name__}: {exc}")

    def execute(self, arguments: dict[str, Any]) -> str:
        return self._search_content(arguments, user_id="local", trace=None)

    def _search_content(self, arguments: dict[str, Any], *, user_id: str, trace: Any) -> str:
        query = arguments.get("query", "")
        max_results = int(arguments.get("max_results", 5))
        results = self.memory.search(
            query,
            limit=max_results,
            user_id=user_id,
            trace=trace,
        )
        if not results:
            return "(no relevant memories found)"

        lines = []
        for index, result in enumerate(results, 1):
            rendered = self.memory.episodic_store.render_record(result.record)
            lines.append(f"{index}. " + rendered.replace("\n", "\n   "))
        return "\n".join(lines)


class ReadMemoryTool(BaseTool):
    name = "read_memory"
    description = "Read the current semantic memory file MEMORY.md."
    parameters = {
        "type": "object",
        "properties": {},
    }
    risk_level = "R0"
    idempotent = True

    def __init__(self, workspace: Path, memory: MemoryManager):
        super().__init__(workspace)
        self.memory = memory

    def run(self, arguments: dict[str, Any], context: Any = None) -> ToolResult:
        try:
            content = self.memory.read_main_memory(user_id="local")
            return ToolResult(ok=True, content=content or "(memory is empty)")
        except Exception as exc:
            return ToolResult(ok=False, content=f"{type(exc).__name__}: {exc}")

    def execute(self, arguments: dict[str, Any]) -> str:
        content = self.memory.read_main_memory(user_id="local")
        return content or "(memory is empty)"
