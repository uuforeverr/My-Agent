import io
import json
import os
import urllib.error

import pytest

from config import Settings, load_settings
from llm.openai_client import LLMError, OpenAICompatibleClient


def _settings(tmp_path, key="key"):
    return Settings(
        project_root=tmp_path,
        openai_api_key=key,
        openai_api_base="https://example.test/v1",
        model="fake-model",
        workspace=tmp_path / "workspace",
    )


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def test_chat_requires_api_key(tmp_path):
    client = OpenAICompatibleClient(_settings(tmp_path, key=""))
    with pytest.raises(LLMError):
        client.chat([{"role": "user", "content": "hi"}])


def test_chat_success(monkeypatch, tmp_path):
    def fake_urlopen(req, timeout):
        assert timeout == 120
        body = json.loads(req.data.decode("utf-8"))
        assert body["model"] == "fake-model"
        return FakeResponse({"choices": [{"message": {"role": "assistant", "content": "ok"}}]})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = OpenAICompatibleClient(_settings(tmp_path))
    message = client.chat([{"role": "user", "content": "hi"}], tools=[{"x": 1}])
    assert message["content"] == "ok"


def test_chat_records_response_metadata(monkeypatch, tmp_path):
    def fake_urlopen(req, timeout):
        return FakeResponse({
            "model": "fake-model-2026",
            "usage": {
                "prompt_tokens": 11,
                "completion_tokens": 7,
                "total_tokens": 18,
            },
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "ok"},
                }
            ],
        })

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = OpenAICompatibleClient(_settings(tmp_path))
    client.chat([{"role": "user", "content": "hi"}])

    assert client.last_response_metadata == {
        "model": "fake-model-2026",
        "usage": {
            "prompt_tokens": 11,
            "completion_tokens": 7,
            "total_tokens": 18,
        },
        "finish_reason": "stop",
    }


def test_chat_unexpected_response(monkeypatch, tmp_path):
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout: FakeResponse({"bad": []}))
    client = OpenAICompatibleClient(_settings(tmp_path))
    with pytest.raises(LLMError) as exc:
        client.chat([{"role": "user", "content": "hi"}])
    assert "Unexpected" in str(exc.value)


def test_chat_http_error(monkeypatch, tmp_path):
    def fake_urlopen(req, timeout):
        raise urllib.error.HTTPError(
            url=req.full_url,
            code=401,
            msg="Unauthorized",
            hdrs=None,
            fp=io.BytesIO(b'{"error":"bad key"}'),
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    client = OpenAICompatibleClient(_settings(tmp_path))
    with pytest.raises(LLMError) as exc:
        client.chat([{"role": "user", "content": "hi"}])
    assert "LLM HTTP 401" in str(exc.value)


@pytest.mark.live
def test_live_deepseek_smoke(monkeypatch):
    if os.environ.get("RUN_LIVE_LLM") != "1":
        pytest.skip("set RUN_LIVE_LLM=1 to run real DeepSeek smoke test")
    settings = load_settings()
    client = OpenAICompatibleClient(settings)
    message = client.chat([{"role": "user", "content": "只回复 pong"}])
    assert message.get("content") or message.get("tool_calls")
