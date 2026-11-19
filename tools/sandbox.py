from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from tools.base import ToolError


SANDBOX_PROFILES = ("read_only", "workspace_write")
DEFAULT_SANDBOX_IMAGE = "my-agent-sandbox:py311-v1"


@dataclass(frozen=True)
class SandboxResult:
    exit_code: int
    stdout: str
    stderr: str


class SandboxExecutor(Protocol):
    def execute(self, command: str, *, profile: str, timeout: int) -> SandboxResult:
        ...


class DockerSandboxExecutor:
    """Run untrusted shell commands in a short-lived, least-privilege container."""

    def __init__(
        self,
        workspace: Path,
        *,
        enabled: bool = True,
        docker_binary: str = "docker",
        image: str = DEFAULT_SANDBOX_IMAGE,
        network: str = "none",
        memory: str = "512m",
        cpus: float = 1.0,
        pids_limit: int = 128,
        user: str = "65532:65532",
        tmpfs_size: str = "64m",
    ):
        self.workspace = workspace.resolve()
        self.enabled = bool(enabled)
        self.docker_binary = str(docker_binary or "docker")
        self.image = str(image or DEFAULT_SANDBOX_IMAGE)
        self.network = str(network or "none")
        self.memory = str(memory or "512m")
        self.cpus = max(float(cpus), 0.1)
        self.pids_limit = max(int(pids_limit), 16)
        self.user = str(user or "")
        self.tmpfs_size = str(tmpfs_size or "64m")

    def execute(self, command: str, *, profile: str, timeout: int) -> SandboxResult:
        if not self.enabled:
            raise ToolError("Docker sandbox is disabled; refusing host execution")
        if profile not in SANDBOX_PROFILES:
            raise ToolError(f"unknown sandbox profile: {profile}")
        if not command.strip():
            raise ToolError("command is required")

        argv = self.build_argv(command, profile=profile)
        try:
            completed = subprocess.run(
                argv,
                shell=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=max(int(timeout), 1),
            )
        except FileNotFoundError as error:
            raise ToolError(
                "Docker CLI was not found; install/start Docker before using bash"
            ) from error
        except subprocess.TimeoutExpired as error:
            raise ToolError(f"sandbox command timed out after {timeout}s") from error
        except OSError as error:
            raise ToolError(f"failed to start Docker sandbox: {error}") from error

        stderr = completed.stderr or ""
        if completed.returncode == 125:
            detail = stderr.strip() or "Docker could not start the sandbox"
            raise ToolError(f"Docker sandbox unavailable: {detail}")
        return SandboxResult(
            exit_code=int(completed.returncode),
            stdout=completed.stdout or "",
            stderr=stderr,
        )

    def build_argv(self, command: str, *, profile: str) -> list[str]:
        if profile not in SANDBOX_PROFILES:
            raise ToolError(f"unknown sandbox profile: {profile}")
        mount = f"type=bind,src={self.workspace},dst=/workspace"
        if profile == "read_only":
            mount += ",readonly"

        argv = [
            self.docker_binary,
            "run",
            "--rm",
            "--network",
            self.network,
            "--read-only",
            "--tmpfs",
            f"/tmp:rw,noexec,nosuid,size={self.tmpfs_size}",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            str(self.pids_limit),
            "--memory",
            self.memory,
            "--cpus",
            str(self.cpus),
            "--mount",
            mount,
            "--workdir",
            "/workspace",
        ]
        if self.user:
            argv.extend(["--user", self.user])
        argv.extend([self.image, "/bin/sh", "-lc", command])
        return argv
