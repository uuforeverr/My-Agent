import sys
from pathlib import Path
from typing import Any, Optional

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import Settings


@pytest.fixture
def otel_exporter(tmp_path: Path):
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    import observability.otel as otel

    otel.shutdown_observability()
    settings = type("OtelSettings", (), {
        "project_root": tmp_path,
        "otel_enabled": False,
        "otel_capture_content": True,
        "otel_content_max_bytes": 1_000_000,
        "otel_service_name": "my-agent-test",
        "otel_environment": "test",
    })()
    exporter = InMemorySpanExporter()
    provider = otel.configure_observability(settings)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    yield exporter
    provider.force_flush()
    otel.shutdown_observability()


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
        if "keep_recent_turns" in overrides:
            overrides["session_window_turns"] = overrides.pop("keep_recent_turns")
        overrides.pop("max_history_messages", None)
        overrides.pop("memory_flush_messages", None)
        overrides.pop("episodic_scan_interval", None)
        overrides.pop("episodic_flush_min_turns", None)
        overrides.pop("daily_dream_lookback_days", None)
        Path(workspace).mkdir(parents=True, exist_ok=True)
        defaults: dict[str, Any] = {
            "project_root": tmp_path,
            "openai_api_key": "test-key",
            "openai_api_base": "https://example.test/v1",
            "model": "fake-model",
            "workspace": Path(workspace),
            "max_steps": 6,
            "session_window_turns": 20,
            "memory_search_results": 5,
            "memory_embedding_provider": "disabled",
            "bash_timeout": 20,
            "channels": ("cli",),
            "feishu_app_id": "",
            "feishu_app_secret": "",
            "qianfan_api_key": "",
            "qianfan_api_base": "https://qianfan.baidubce.com/v2",
            "web_search_timeout": 15,
            "scheduler_enabled": True,
            "scheduler_scan_interval": 30,
            "outbox_relay_interval_seconds": 15.0,
            "outbox_relay_batch_size": 20,
            "outbox_delivery_lease_seconds": 120.0,
            "scheduler_tasks_path": Path(workspace).parent / "data" / "scheduler" / "tasks.json",
            "daily_dream_enabled": True,
            "daily_dream_cron": "0 0 * * *",
            "tool_approval_mode": "risk_based",
            "model_context_window": None,
            "raw_write_batch_size": 32,
            "raw_write_flush_interval_ms": 10,
            "raw_write_queue_capacity": 4096,
            "episodic_batch_turns": 20,
            "episodic_max_wait_seconds": 3600,
            "episodic_worker_poll_seconds": 60,
            "episodic_body_max_chars": 60,
        }
        defaults.update(overrides)
        return Settings(**defaults)

    return _make
