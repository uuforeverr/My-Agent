import re
from pathlib import Path
from typing import Any

from tools.base import BaseTool, ToolError
from tools.sandbox import DockerSandboxExecutor, SANDBOX_PROFILES, SandboxExecutor

MAX_BASH_TIMEOUT = 30
class BashTool(BaseTool):
    name = "bash"
    description = (
        "Run a short, non-interactive shell command inside a locked-down Docker sandbox. "
        "The default profile mounts the workspace read-only; request workspace_write "
        "only when files must change. Network access is disabled. Long output is spilled to a file; "
        "continue with read_file offset/limit. "
        "Use for tests, searches, formatters, and diagnostics."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": (
                    "Shell command run inside /workspace in an ephemeral container."
                ),
            },
            "profile": {
                "type": "string",
                "enum": list(SANDBOX_PROFILES),
                "description": "read_only (default) or workspace_write.",
            },
            "timeout": {
                "type": "integer",
                "description": f"Timeout in seconds. Maximum is {MAX_BASH_TIMEOUT}.",
            },
        },
        "required": ["command"],
    }
    risk_level = "R2"
    idempotent = False

    def __init__(
        self,
        workspace: Path,
        default_timeout: int = 20,
        executor: SandboxExecutor | None = None,
        **sandbox_options: Any,
    ):
        super().__init__(workspace)
        self.default_timeout = min(max(int(default_timeout), 1), MAX_BASH_TIMEOUT)
        self.executor = executor or DockerSandboxExecutor(workspace, **sandbox_options)

    def risk_for(self, arguments: dict[str, Any]) -> str:
        return "R1" if arguments.get("profile", "read_only") == "read_only" else "R2"

    def execute(self, arguments: dict[str, Any]) -> str:
        command = str(arguments.get("command", "")).strip()
        timeout = self._resolve_timeout(arguments.get("timeout"))
        profile = str(arguments.get("profile", "read_only")).strip()

        if not command:
            raise ToolError("command is required")
        if profile not in SANDBOX_PROFILES:
            raise ToolError(f"unknown sandbox profile: {profile}")
        self._check_hard_denies(command)
        result = self.executor.execute(command, profile=profile, timeout=timeout)

        if result.exit_code != 0:
            raise ToolError(
                f"sandbox command exited with code {result.exit_code}",
                error_code="command_failed",
                error_type="execution_error",
                retryable=True,
                details={
                    "exit_code": result.exit_code,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "sandbox_profile": profile,
                },
                suggested_action="Inspect stdout/stderr, correct the command, and retry if appropriate",
            )

        output = result.stdout
        if result.stderr:
            output = output + ("\n" if output else "") + result.stderr
        output = output.strip() or "(no output)"

        return "\n".join(
            [
                f"sandbox_profile={profile}",
                f"exit_code={result.exit_code}",
                "",
                output,
            ]
        )

    def _resolve_timeout(self, raw_timeout: Any) -> int:
        if raw_timeout is None:
            return self.default_timeout
        return min(max(int(raw_timeout), 1), MAX_BASH_TIMEOUT)

    def _check_hard_denies(self, command: str) -> None:
        lowered = command.lower()
        if ".env" in lowered or "openai_api_key" in lowered or "api_key" in lowered:
            raise ToolError("refusing to run command that may expose credentials")
        if re.search(r"\brm\s+(?:-[a-z]*r[a-z]*f[a-z]*|-[a-z]*f[a-z]*r[a-z]*)\s+(?:\./?|/workspace/?|\*)\s*$", lowered):
            raise ToolError("refusing to delete the workspace root")
