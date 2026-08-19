import sys
from pathlib import Path
from typing import Any, Optional

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import Settings


class FakeLLM:
    """Offline stand-in for OpenAICompatibleClient.

    Provide either a single response dict/str, or a list of scripted responses
    returned one per call. Records every messages payload it receives.
    """

    def __init__(self, responses: Any = None):
        if responses is None:
            responses = {"role": "assistant", "content": "ok"}
        if not isinstance(responses, list):
            responses = [responses]
        self._responses = responses
        self.calls: list[list[dict[str, Any]]] = []
        self._index = 0

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        self.calls.append(messages)
        if self._index < len(self._responses):
            response = self._responses[self._index]
        else:
            response = self._responses[-1]
        self._index += 1
        if isinstance(response, str):
            return {"role": "assistant", "content": response}
        return response


@pytest.fixture
def tmp_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace


@pytest.fixture
def tmp_memory_dir(tmp_path: Path) -> Path:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    return memory_dir


@pytest.fixture
def settings_factory(tmp_path: Path):
    def _make(**overrides: Any) -> Settings:
        workspace = overrides.pop("workspace", tmp_path / "workspace")
        Path(workspace).mkdir(parents=True, exist_ok=True)
        defaults: dict[str, Any] = {
            "project_root": tmp_path,
            "openai_api_key": "test-key",
            "openai_api_base": "https://example.test/v1",
            "model": "fake-model",
            "workspace": Path(workspace),
            "max_steps": 6,
            "max_history_messages": 40,
            "memory_search_results": 5,
            "memory_flush_messages": 12,
            "bash_timeout": 20,
            "channels": ("cli",),
            "feishu_app_id": "",
            "feishu_app_secret": "",
            "qianfan_api_key": "",
            "qianfan_api_base": "https://qianfan.baidubce.com/v2",
            "web_search_timeout": 15,
            "scheduler_enabled": True,
            "scheduler_scan_interval": 30,
            "scheduler_catchup_seconds": 600,
            "scheduler_tasks_path": Path(workspace).parent / "data" / "scheduler" / "tasks.json",
            "daily_dream_enabled": True,
            "daily_dream_cron": "0 0 * * *",
            "daily_dream_lookback_days": 1,
            "tool_approval_mode": "off",
            "model_context_window": None,
            "keep_recent_turns": 3,
            "episodic_scan_interval": 60,
            "episodic_flush_min_turns": 20,
        }
        defaults.update(overrides)
        return Settings(**defaults)

    return _make
