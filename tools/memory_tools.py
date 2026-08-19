from pathlib import Path
from typing import Any

from memory import MemoryManager
from tools.base import BaseTool


class RememberMemoryTool(BaseTool):
    name = "remember_memory"
    description = "Save a durable semantic memory into MEMORY.md."
    parameters = {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                    "description": "The concise durable fact, preference, rule, or project decision to remember semantically.",
            }
        },
        "required": ["content"],
    }
    risk_level = "L1"
    idempotent = False

    def __init__(self, workspace: Path, memory: MemoryManager):
        super().__init__(workspace)
        self.memory = memory

    def execute(self, arguments: dict[str, Any]) -> str:
        return self.memory.remember(arguments.get("content", ""), source=self.name)


class SearchMemoryTool(BaseTool):
    name = "memory_search"
    description = "Search semantic memory and episodic daily memory by natural language query."
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
    risk_level = "L0"
    idempotent = True

    def __init__(self, workspace: Path, memory: MemoryManager):
        super().__init__(workspace)
        self.memory = memory

    def execute(self, arguments: dict[str, Any]) -> str:
        query = arguments.get("query", "")
        max_results = int(arguments.get("max_results", 5))
        results = self.memory.search(query, limit=max_results)
        if not results:
            return "(no relevant memories found)"

        lines = []
        for index, result in enumerate(results, 1):
            lines.append(
                f"{index}. {result.record.content} "
                f"(id={result.record.id}, score={result.score:.2f})"
            )
        return "\n".join(lines)


class ReadMemoryTool(BaseTool):
    name = "read_memory"
    description = "Read the current semantic memory file MEMORY.md."
    parameters = {
        "type": "object",
        "properties": {},
    }
    risk_level = "L0"
    idempotent = True

    def __init__(self, workspace: Path, memory: MemoryManager):
        super().__init__(workspace)
        self.memory = memory

    def execute(self, arguments: dict[str, Any]) -> str:
        content = self.memory.read_main_memory()
        return content or "(memory is empty)"
