from pathlib import Path

from tools.bash_tool import MAX_BASH_TIMEOUT, BashTool
from tools.sandbox import DEFAULT_SANDBOX_IMAGE, DockerSandboxExecutor, SandboxResult


class _FakeSandbox:
    def __init__(self, result: SandboxResult | None = None):
        self.result = result or SandboxResult(0, "hello\n", "")
        self.calls = []

    def execute(self, command: str, *, profile: str, timeout: int) -> SandboxResult:
        self.calls.append((command, profile, timeout))
        return self.result


def test_bash_delegates_to_read_only_sandbox_by_default(tmp_workspace: Path):
    executor = _FakeSandbox()
    tool = BashTool(tmp_workspace, default_timeout=10, executor=executor)

    result = tool.run({"command": "python -c \"print('hello')\""})

    assert result.ok
    assert "sandbox_profile=read_only" in result.content
    assert "exit_code=0" in result.content
    assert "hello" in result.content
    assert executor.calls == [("python -c \"print('hello')\"", "read_only", 10)]


def test_bash_workspace_write_is_explicit_and_r2(tmp_workspace: Path):
    executor = _FakeSandbox()
    tool = BashTool(tmp_workspace, executor=executor)
    args = {"command": "touch a.txt", "profile": "workspace_write"}

    result = tool.run(args)

    assert result.ok
    assert tool.risk_for(args) == "R2"
    assert executor.calls[0][1] == "workspace_write"


def test_bash_reports_nonzero_exit(tmp_workspace: Path):
    executor = _FakeSandbox(SandboxResult(3, "", "failed"))
    tool = BashTool(tmp_workspace, executor=executor)
    result = tool.run({"command": "false"})
    assert result.ok is False
    assert result.error_code == "command_failed"
    assert result.retryable is True
    assert result.details["exit_code"] == 3
    assert result.details["stderr"] == "failed"


def test_bash_accepts_shell_pipelines_inside_sandbox(tmp_workspace: Path):
    executor = _FakeSandbox()
    tool = BashTool(tmp_workspace, executor=executor)
    result = tool.run({"command": "printf x | wc -c"})
    assert result.ok
    assert executor.calls[0][0] == "printf x | wc -c"


def test_bash_rejects_unknown_profile(tmp_workspace: Path):
    tool = BashTool(tmp_workspace, executor=_FakeSandbox())
    result = tool.run({"command": "true", "profile": "host"})
    assert result.ok is False
    assert "unknown sandbox profile" in result.content


def test_bash_blocks_env_access(tmp_workspace: Path):
    tool = BashTool(tmp_workspace, executor=_FakeSandbox())
    result = tool.run({"command": "cat .env"})
    assert result.ok is False
    assert "credential" in result.content.lower()


def test_bash_blocks_api_key(tmp_workspace: Path):
    tool = BashTool(tmp_workspace, executor=_FakeSandbox())
    result = tool.run({"command": "echo $OPENAI_API_KEY"})
    assert result.ok is False


def test_bash_blocks_rm_rf_workspace(tmp_workspace: Path):
    tool = BashTool(tmp_workspace, executor=_FakeSandbox())
    for command in ("rm -rf .", "rm -fr /workspace", "rm -rf *"):
        result = tool.run({"command": command, "profile": "workspace_write"})
        assert result.ok is False
        assert "workspace root" in result.content.lower()


def test_bash_timeout_capped(tmp_workspace: Path):
    tool = BashTool(tmp_workspace, default_timeout=999, executor=_FakeSandbox())
    assert tool.default_timeout == MAX_BASH_TIMEOUT
    assert tool._resolve_timeout(999) == MAX_BASH_TIMEOUT
    assert tool._resolve_timeout(None) == MAX_BASH_TIMEOUT


def test_docker_argv_enforces_container_boundaries(tmp_workspace: Path):
    executor = DockerSandboxExecutor(tmp_workspace, image="test-image:latest")
    argv = executor.build_argv("python -V", profile="read_only")

    assert argv[:3] == ["docker", "run", "--rm"]
    assert argv[argv.index("--network") + 1] == "none"
    assert "--read-only" in argv
    assert ["--cap-drop", "ALL"] == argv[argv.index("--cap-drop"):argv.index("--cap-drop") + 2]
    assert "no-new-privileges" in argv
    mount = argv[argv.index("--mount") + 1]
    assert "dst=/workspace" in mount and mount.endswith(",readonly")
    assert argv[-4:] == ["test-image:latest", "/bin/sh", "-lc", "python -V"]


def test_docker_write_profile_only_changes_workspace_mount(tmp_workspace: Path):
    executor = DockerSandboxExecutor(tmp_workspace)
    argv = executor.build_argv("touch x", profile="workspace_write")
    mount = argv[argv.index("--mount") + 1]
    assert "dst=/workspace" in mount
    assert not mount.endswith(",readonly")
    assert "--read-only" in argv


def test_docker_uses_project_owned_image_by_default(tmp_workspace: Path):
    executor = DockerSandboxExecutor(tmp_workspace)
    argv = executor.build_argv("python -V", profile="read_only")

    assert executor.image == DEFAULT_SANDBOX_IMAGE
    assert DEFAULT_SANDBOX_IMAGE in argv
