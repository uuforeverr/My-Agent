from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from agent.run_context import RunContext


@dataclass
class ToolResult:
    ok: bool
    content: str
    waiting_approval: bool = False
    approval_id: str = ""


class ToolError(RuntimeError):
    pass


class BaseTool:
    name = "base"
    description = "Base tool"
    parameters: dict[str, Any] = {"type": "object", "properties": {}}
    risk_level = "L0"
    requires_approval: Optional[bool] = None
    idempotent = True

    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve()

    def risk_for(self, arguments: dict[str, Any]) -> str:
        return self.risk_level

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def run(
        self,
        arguments: dict[str, Any],
        context: "Optional[RunContext]" = None,
    ) -> ToolResult:
        try:
            return ToolResult(ok=True, content=self.execute(arguments))
        except Exception as e:
            return ToolResult(ok=False, content=f"{type(e).__name__}: {e}")

    def execute(self, arguments: dict[str, Any]) -> str:
        raise NotImplementedError

    def resolve_workspace_path(self, raw_path: str) -> Path:
        if not raw_path:
            raise ToolError("path is required")
        path = (self.workspace / raw_path).resolve()
        if path != self.workspace and self.workspace not in path.parents:
            raise ToolError(f"path escapes workspace: {raw_path}")
        return path
