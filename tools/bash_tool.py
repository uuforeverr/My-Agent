import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any

from tools.base import BaseTool, ToolError


MAX_BASH_OUTPUT_CHARS = 20000
MAX_BASH_TIMEOUT = 30


class BashTool(BaseTool):
    name = "bash"
    description = (
        "Run a short, non-interactive shell command inside the workspace. "
        "Use for tests, searches, formatters, and diagnostics."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Shell command to run from the workspace directory.",
            },
            "timeout": {
                "type": "integer",
                "description": f"Timeout in seconds. Maximum is {MAX_BASH_TIMEOUT}.",
            },
        },
        "required": ["command"],
    }

    def __init__(self, workspace: Path, default_timeout: int = 20):
        super().__init__(workspace)
        self.default_timeout = min(max(int(default_timeout), 1), MAX_BASH_TIMEOUT)

    def execute(self, arguments: dict[str, Any]) -> str:
        command = str(arguments.get("command", "")).strip()
        timeout = self._resolve_timeout(arguments.get("timeout"))

        if not command:
            raise ToolError("command is required")

        self._check_safety(command)

        result = subprocess.run(
            command,
            shell=True,
            cwd=str(self.workspace),
            env=self._safe_env(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )

        output = result.stdout
        if result.stderr:
            output = output + ("\n" if output else "") + result.stderr
        output = output.strip() or "(no output)"
        output, truncated = self._truncate_output(output)

        lines = [
            f"exit_code={result.returncode}",
            "",
            output,
        ]
        if truncated:
            lines.append(f"\n[output truncated to {MAX_BASH_OUTPUT_CHARS} characters]")
        return "\n".join(lines)

    def _resolve_timeout(self, raw_timeout: Any) -> int:
        if raw_timeout is None:
            return self.default_timeout
        return min(max(int(raw_timeout), 1), MAX_BASH_TIMEOUT)

    def _safe_env(self) -> dict[str, str]:
        allowed_keys = {
            "HOME",
            "LANG",
            "LC_ALL",
            "PATH",
            "PYTHONPATH",
            "VIRTUAL_ENV",
        }
        return {
            key: value
            for key, value in os.environ.items()
            if key in allowed_keys
        }

    def _check_safety(self, command: str) -> None:
        lowered = command.lower()
        if ".env" in lowered or "openai_api_key" in lowered or "api_key" in lowered:
            raise ToolError("refusing to run command that may expose credentials")

        if re.search(r"\b(sudo|su|shutdown|reboot|halt|poweroff|mkfs|diskutil|launchctl)\b", lowered):
            raise ToolError("refusing to run privileged or system-level command")

        if re.search(r"\bdd\b.*\b(if|of)=", lowered):
            raise ToolError("refusing to run disk-writing command")

        if re.search(r"\b(curl|wget)\b.*\|\s*(sh|bash|zsh|python|python3)\b", lowered):
            raise ToolError("refusing to pipe downloaded content into an interpreter")

        for token in self._tokens(command):
            if token.startswith("/") or token.startswith("~"):
                raise ToolError(f"absolute or home path is not allowed in bash: {token}")
            if ".." in Path(token).parts:
                raise ToolError(f"path traversal is not allowed in bash: {token}")

        if self._deletes_workspace_root(command):
            raise ToolError("refusing to delete the workspace root")

    def _tokens(self, command: str) -> list[str]:
        try:
            return shlex.split(command)
        except ValueError:
            return command.split()

    def _deletes_workspace_root(self, command: str) -> bool:
        tokens = self._tokens(command.lower())
        for index, token in enumerate(tokens):
            if token != "rm":
                continue
            has_recursive_force = False
            for next_token in tokens[index + 1:]:
                if next_token.startswith("-") and "r" in next_token and "f" in next_token:
                    has_recursive_force = True
                    continue
                if next_token in {".", "./", "*", "./*"} and has_recursive_force:
                    return True
                break
        return False

    def _truncate_output(self, output: str) -> tuple[str, bool]:
        if len(output) <= MAX_BASH_OUTPUT_CHARS:
            return output, False
        return output[-MAX_BASH_OUTPUT_CHARS:], True
