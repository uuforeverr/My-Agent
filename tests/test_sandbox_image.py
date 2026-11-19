from pathlib import Path

from tools.sandbox import DEFAULT_SANDBOX_IMAGE


def test_sandbox_dockerfile_pins_base_and_required_tools():
    root = Path(__file__).resolve().parent.parent
    dockerfile = (root / "docker" / "sandbox" / "Dockerfile").read_text(encoding="utf-8")

    assert "python:3.11-slim@sha256:" in dockerfile
    assert "ripgrep" in dockerfile
    assert 'pytest==${PYTEST_VERSION}' in dockerfile
    assert DEFAULT_SANDBOX_IMAGE == "my-agent-sandbox:py311-v1"
