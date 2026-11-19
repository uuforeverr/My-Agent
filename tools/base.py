from dataclasses import dataclass, field
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
    metadata: dict[str, Any] = field(default_factory=dict)
    error_code: str = ""
    error_type: str = ""
    retryable: bool = False
    details: dict[str, Any] = field(default_factory=dict)
    suggested_action: str = ""

    def function_output(self, tool_name: str, call_id: str) -> str:
        import json

        if self.ok:
            payload: dict[str, Any] = {
                "ok": True,
                "tool": tool_name,
                "call_id": call_id,
                "data": {"content": self.content},
            }
            if self.metadata:
                payload["metadata"] = self.metadata
        else:
            payload = {
                "ok": False,
                "tool": tool_name,
                "call_id": call_id,
                "error": {
                    "code": self.error_code or "execution_error",
                    "type": self.error_type or "tool_error",
                    "message": self.content,
                    "retryable": bool(self.retryable),
                    "details": self.details,
                    "suggested_action": self.suggested_action or (
                        "Generate a corrected tool call" if self.retryable else "Explain the failure to the user"
                    ),
                },
            }
        return json.dumps(payload, ensure_ascii=False, default=str)


class ToolError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        error_code: str = "",
        error_type: str = "tool_error",
        retryable: bool = True,
        details: Optional[dict[str, Any]] = None,
        suggested_action: str = "",
    ):
        super().__init__(message)
        self.error_code = error_code
        self.error_type = error_type
        self.retryable = retryable
        self.details = details or {}
        self.suggested_action = suggested_action


class BaseTool:
    name = "base"
    description = "Base tool"
    parameters: dict[str, Any] = {"type": "object", "properties": {}}
    risk_level = "R0"
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
        except ToolError as e:
            message = str(e)
            code = e.error_code or ("not_found" if "not found" in message.lower() else "execution_error")
            return ToolResult(
                ok=False,
                content=f"{type(e).__name__}: {e}",
                error_code=code,
                error_type=e.error_type,
                retryable=e.retryable,
                details=e.details,
                suggested_action=e.suggested_action or "Adjust the arguments or use another tool, then retry",
            )
        except (TimeoutError, ConnectionError) as e:
            return ToolResult(
                ok=False,
                content=f"{type(e).__name__}: {e}",
                error_code="network_timeout",
                error_type="transient_error",
                retryable=True,
                suggested_action="Retry with corrected parameters or explain the network failure",
            )
        except Exception as e:
            return ToolResult(
                ok=False,
                content=f"{type(e).__name__}: {e}",
                error_code="execution_error",
                error_type=type(e).__name__,
                retryable=True,
                suggested_action="Inspect the error and generate a corrected tool call",
            )

    def execute(self, arguments: dict[str, Any]) -> str:
        raise NotImplementedError

    def resolve_workspace_path(self, raw_path: str) -> Path:
        if not raw_path:
            raise ToolError("path is required")
        path = (self.workspace / raw_path).resolve()
        if path != self.workspace and self.workspace not in path.parents:
            raise ToolError(f"path escapes workspace: {raw_path}")
        return path
