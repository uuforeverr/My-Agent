from types import SimpleNamespace

import pytest

from config import Settings
from llm.openai_client import LLMError, OpenAICompatibleClient


def _settings(tmp_path, key="key"):
    return Settings(
        project_root=tmp_path,
        openai_api_key=key,
        openai_api_base="https://example.test/v1",
        model="fake-model",
        workspace=tmp_path / "workspace",
    )


class _Responses:
    def __init__(self, events):
        self.events = events
        self.requests = []

    def create(self, **request):
        self.requests.append(request)
        return iter(self.events)


class _SDK:
    def __init__(self, events):
        self.responses = _Responses(events)


def _event(event_type, sequence_number, **values):
    return SimpleNamespace(type=event_type, sequence_number=sequence_number, **values)


def _completed(output, usage=None):
    response = {
        "id": "resp_1",
        "model": "fake-model-2026",
        "status": "completed",
        "output": output,
        "usage": usage or {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
    }
    return _event("response.completed", 3, response=response)


def test_respond_requires_api_key_without_injected_client(tmp_path):
    client = OpenAICompatibleClient(_settings(tmp_path, key=""))
    with pytest.raises(LLMError):
        client.respond([{"role": "user", "content": "hi"}])


def test_respond_streams_text_and_uses_responses_contract(tmp_path):
    output = [
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "ok"}],
        }
    ]
    sdk = _SDK([
        _event("response.created", 1),
        _event("response.output_text.delta", 2, delta="ok"),
        _completed(output),
    ])
    emitted = []
    client = OpenAICompatibleClient(_settings(tmp_path), client=sdk)

    response = client.respond(
        [{"role": "system", "content": "be brief"}, {"role": "user", "content": "hi"}],
        tools=[{
            "type": "function",
            "function": {
                "name": "lookup",
                "description": "look up data",
                "parameters": {"type": "object", "properties": {}},
            },
        }],
        on_event=emitted.append,
    )

    assert response.message["content"] == "ok"
    assert response.output_items == output
    request = sdk.responses.requests[0]
    assert request["stream"] is True
    assert request["store"] is False
    assert request["parallel_tool_calls"] is False
    assert request["instructions"] == "be brief"
    assert request["input"] == [{"role": "user", "content": "hi"}]
    assert request["tools"][0]["name"] == "lookup"
    assert emitted[0]["type"] == "output_text_delta"
    assert emitted[-1]["type"] == "response_completed"
    assert client.last_response_metadata["usage"]["prompt_tokens"] == 11


def test_function_call_is_executably_visible_only_after_completed_event(tmp_path):
    call = {
        "type": "function_call",
        "id": "fc_1",
        "call_id": "call_1",
        "name": "read_file",
        "arguments": '{"path":"README.md"}',
    }
    sdk = _SDK([
        _event("response.function_call_arguments.delta", 1, call_id="call_1", delta='{"path":'),
        _event("response.function_call_arguments.delta", 2, call_id="call_1", delta='"README.md"}'),
        _completed([call]),
    ])
    emitted = []
    response = OpenAICompatibleClient(_settings(tmp_path), client=sdk).respond(
        [{"role": "user", "content": "read it"}], on_event=emitted.append
    )

    assert response.message["tool_calls"][0]["function"]["arguments"] == '{"path":"README.md"}'
    assert [event["type"] for event in emitted[:2]] == ["tool_call_delta", "tool_call_delta"]


def test_incomplete_stream_never_returns_partial_tool_call(tmp_path):
    sdk = _SDK([
        _event("response.function_call_arguments.delta", 1, call_id="call_1", delta='{"path":'),
    ])
    client = OpenAICompatibleClient(_settings(tmp_path), client=sdk)

    with pytest.raises(LLMError, match="before response.completed"):
        client.respond([{"role": "user", "content": "read it"}])


def test_replays_full_response_items_and_structured_tool_output(tmp_path):
    preserved = {"type": "reasoning", "id": "rs_1", "summary": []}
    _instructions, items = OpenAICompatibleClient._to_responses_input([
        {"role": "assistant", "content": "", "response_items": [preserved]},
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": '{"ok":false,"error":{"code":"not_found"}}',
        },
    ])

    assert items[0] == preserved
    assert items[1] == {
        "type": "function_call_output",
        "call_id": "call_1",
        "output": '{"ok":false,"error":{"code":"not_found"}}',
    }


def test_provider_failure_is_structured_as_llm_error(tmp_path):
    sdk = _SDK([_event("response.failed", 1, response={"error": {"code": "bad_request"}})])
    client = OpenAICompatibleClient(_settings(tmp_path), client=sdk)
    with pytest.raises(LLMError, match="response.failed"):
        client.respond([{"role": "user", "content": "hi"}])
