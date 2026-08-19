import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

from tools.base import BaseTool, ToolError

MAX_BASH_TIMEOUT = 30
ALLOWED_COMMANDS = frozenset({
    "python",
    "python3",
    "py",
    "pytest",
    "pip",
    "git",
    "rg",
    "node",
    "npm",
    "npx",
    "ruff",
    "mypy",
})
_META_CHARS = frozenset("|;&><`")


class BashTool(BaseTool):
    name = "bash"
    description = (
        "Run a short, non-interactive allowlisted command inside the workspace. "
        "No pipes, redirects, or command chaining. Do not use echo or other shell "
        "builtins; use python -c for short prints. Long output is spilled to a file; "
        "continue with read_file offset/limit. "
        "Use for tests, searches, formatters, and diagnostics."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": (
                    "Allowlisted executable plus arguments, run from the workspace "
                    "directory. No pipes or redirects."
                ),
            },
            "timeout": {
                "type": "integer",
                "description": f"Timeout in seconds. Maximum is {MAX_BASH_TIMEOUT}.",
            },
        },
        "required": ["command"],
    }
    risk_level = "L2"
    idempotent = False

    def __init__(self, workspace: Path, default_timeout: int = 20):
        super().__init__(workspace)
        self.default_timeout = min(max(int(default_timeout), 1), MAX_BASH_TIMEOUT)

    def execute(self, arguments: dict[str, Any]) -> str:
        command = str(arguments.get("command", "")).strip()
        timeout = self._resolve_timeout(arguments.get("timeout"))

        if not command:
            raise ToolError("command is required")

        argv = self._parse_argv(command)
        self._check_safety(command, argv)

        env = self._safe_env()
        resolved = shutil.which(argv[0], path=env.get("PATH"))
        if not resolved:
            raise ToolError(f"command not found: {argv[0]}")

        result = subprocess.run(
            [resolved, *argv[1:]],
            shell=False,
            cwd=str(self.workspace),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )

        output = result.stdout
        if result.stderr:
            output = output + ("\n" if output else "") + result.stderr
        output = output.strip() or "(no output)"

        return "\n".join(
            [
                f"exit_code={result.returncode}",
                "",
                output,
            ]
        )

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
        if os.name == "nt":
            allowed_keys.update({
                "SYSTEMROOT",
                "SystemRoot",
                "WINDIR",
                "TEMP",
                "TMP",
                "PATHEXT",
                "USERPROFILE",
                "HOMEDRIVE",
                "HOMEPATH",
                "OS",
            })
        return {
            key: value
            for key, value in os.environ.items()
            if key in allowed_keys
        }

    def _parse_argv(self, command: str) -> list[str]:
        try:
            argv = shlex.split(command, posix=True)
        except ValueError as error:
            raise ToolError(f"invalid command quoting: {error}") from error
        if not argv:
            raise ToolError("command is required")
        return argv

    def _check_safety(self, command: str, argv: list[str]) -> None:
        lowered = command.lower()
        if ".env" in lowered or "openai_api_key" in lowered or "api_key" in lowered:
            raise ToolError("refusing to run command that may expose credentials")

        if re.search(r"\b(sudo|su|shutdown|reboot|halt|poweroff|mkfs|diskutil|launchctl)\b", lowered):
            raise ToolError("refusing to run privileged or system-level command")

        if re.search(r"\bdd\b.*\b(if|of)=", lowered):
            raise ToolError("refusing to run disk-writing command")

        if re.search(r"\b(curl|wget)\b.*\|\s*(sh|bash|zsh|python|python3)\b", lowered):
            raise ToolError("refusing to pipe downloaded content into an interpreter")

        for token in argv:
            if self._is_user_absolute(token):
                raise ToolError(f"absolute or home path is not allowed in bash: {token}")
            if ".." in Path(token).parts:
                raise ToolError(f"path traversal is not allowed in bash: {token}")

        if self._deletes_workspace_root(argv):
            raise ToolError("refusing to delete the workspace root")

        if "\n" in command or "\r" in command:
            raise ToolError("pipes, redirects, and command chaining are not allowed")
        for token in argv:
            if any(ch in token for ch in _META_CHARS) or "$(" in token:
                raise ToolError("pipes, redirects, and command chaining are not allowed")

        head = argv[0]
        if head != Path(head).name or "/" in head or "\\" in head:
            raise ToolError(f"absolute or home path is not allowed in bash: {head}")
        if head.lower() not in ALLOWED_COMMANDS:
            raise ToolError(f"command is not allowed: {head}")

    def _is_user_absolute(self, token: str) -> bool:
        if token.startswith("/") or token.startswith("~") or token.startswith("\\\\"):
            return True
        return Path(token).is_absolute()

    def _deletes_workspace_root(self, argv: list[str]) -> bool:
        tokens = [token.lower() for token in argv]
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
