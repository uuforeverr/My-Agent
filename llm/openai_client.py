from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

from config import Settings


class LLMError(RuntimeError):
    """A provider or stream failure that is safe to surface to the agent runtime."""


@dataclass(frozen=True)
class ModelStreamEvent:
    type: str
    delta: str = ""
    sequence_number: int = 0
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "type": self.type,
            "sequence_number": self.sequence_number,
            **self.data,
        }
        if self.delta:
            payload["delta"] = self.delta
        return payload


@dataclass
class ModelResponse:
    message: dict[str, Any]
    output_items: list[dict[str, Any]]
    response_id: str = ""
    model: str = ""
    status: str = "completed"
    usage: dict[str, Any] = field(default_factory=dict)
    time_to_first_event_ms: Optional[float] = None
    time_to_first_output_text_ms: Optional[float] = None
    stream_duration_ms: Optional[float] = None
    last_event_type: str = ""
    event_count: int = 0


StreamCallback = Callable[[dict[str, Any]], None]


class OpenAICompatibleClient:
    """OpenAI Responses API client with a small legacy chat compatibility layer."""

    def __init__(self, settings: Settings, client: Any = None):
        self.settings = settings
        self.last_response_metadata: dict[str, Any] = {}
        self._client = client

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        if not self.settings.openai_api_key:
            raise LLMError("OPENAI_API_KEY is not set. Create .env from .env.example first.")
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - dependency failure
            raise LLMError("The official 'openai' Python package is not installed.") from exc
        self._client = OpenAI(
            api_key=self.settings.openai_api_key,
            base_url=self.settings.openai_api_base,
            timeout=120.0,
            max_retries=2,
        )
        return self._client

    def respond(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        on_event: Optional[StreamCallback] = None,
    ) -> ModelResponse:
        if not self.settings.openai_api_key and self._client is None:
            raise LLMError("OPENAI_API_KEY is not set. Create .env from .env.example first.")

        instructions, input_items = self._to_responses_input(messages)
        request: dict[str, Any] = {
            "model": self.settings.model,
            "input": input_items,
            "stream": True,
            "store": False,
            "parallel_tool_calls": False,
        }
        if instructions:
            request["instructions"] = instructions
        response_tools = self._to_responses_tools(tools or [])
        if response_tools:
            request["tools"] = response_tools
            request["tool_choice"] = "auto"

        started = time.monotonic()
        first_event_at: Optional[float] = None
        first_text_at: Optional[float] = None
        last_event_at: Optional[float] = None
        last_event_type = ""
        event_count = 0
        completed_response: Any = None

        try:
            stream = self._get_client().responses.create(**request)
            for raw_event in stream:
                event_count += 1
                now = time.monotonic()
                if first_event_at is None:
                    first_event_at = now
                last_event_at = now
                event_type = str(self._get(raw_event, "type", ""))
                last_event_type = event_type
                sequence = int(self._get(raw_event, "sequence_number", event_count) or event_count)

                if event_type == "response.output_text.delta":
                    delta = str(self._get(raw_event, "delta", "") or "")
                    if delta and first_text_at is None:
                        first_text_at = now
                    self._emit(on_event, ModelStreamEvent("output_text_delta", delta, sequence))
                elif "reasoning_summary" in event_type and event_type.endswith(".delta"):
                    delta = str(self._get(raw_event, "delta", "") or "")
                    self._emit(on_event, ModelStreamEvent("reasoning_summary_delta", delta, sequence))
                elif event_type == "response.function_call_arguments.delta":
                    self._emit(
                        on_event,
                        ModelStreamEvent(
                            "tool_call_delta",
                            str(self._get(raw_event, "delta", "") or ""),
                            sequence,
                            {
                                "call_id": str(self._get(raw_event, "call_id", "") or ""),
                                "output_index": self._get(raw_event, "output_index", None),
                            },
                        ),
                    )
                elif event_type == "response.completed":
                    completed_response = self._get(raw_event, "response", None)
                elif event_type in {"response.failed", "response.incomplete", "error"}:
                    detail = self._dump(raw_event)
                    self._emit(
                        on_event,
                        ModelStreamEvent(
                            "stream_failed",
                            sequence_number=sequence,
                            data={"provider_event_type": event_type, "error": detail},
                        ),
                    )
                    raise LLMError(f"Responses stream ended with {event_type}: {detail}")
        except LLMError:
            raise
        except Exception as exc:
            self._emit(
                on_event,
                ModelStreamEvent(
                    "stream_failed",
                    sequence_number=event_count,
                    data={"provider_event_type": last_event_type, "error": f"{type(exc).__name__}: {exc}"},
                ),
            )
            raise LLMError(
                f"Responses request failed after event {event_count} ({last_event_type or 'none'}): "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        if completed_response is None:
            raise LLMError(
                "Responses stream closed before response.completed; no tool call was executed "
                f"(last_event={last_event_type or 'none'}, event_count={event_count})"
            )

        response_data = self._dump(completed_response)
        if not isinstance(response_data, dict):
            raise LLMError(f"Unexpected completed Responses payload: {response_data!r}")
        output_items = [item for item in response_data.get("output", []) if isinstance(item, dict)]
        message = self._legacy_message(output_items)
        usage = self._normalize_usage(response_data.get("usage") or {})
        finished = time.monotonic()
        result = ModelResponse(
            message=message,
            output_items=output_items,
            response_id=str(response_data.get("id") or ""),
            model=str(response_data.get("model") or self.settings.model),
            status=str(response_data.get("status") or "completed"),
            usage=usage,
            time_to_first_event_ms=self._elapsed_ms(started, first_event_at),
            time_to_first_output_text_ms=self._elapsed_ms(started, first_text_at),
            stream_duration_ms=self._elapsed_ms(first_event_at, last_event_at or finished),
            last_event_type=last_event_type,
            event_count=event_count,
        )
        result.message["response_items"] = output_items
        self.last_response_metadata = {
            "response_id": result.response_id,
            "model": result.model,
            "status": result.status,
            "usage": usage,
            "finish_reason": "tool_calls" if result.message.get("tool_calls") else "stop",
            "time_to_first_event_ms": result.time_to_first_event_ms,
            "time_to_first_output_text_ms": result.time_to_first_output_text_ms,
            "stream_duration_ms": result.stream_duration_ms,
            "event_count": result.event_count,
            "last_event_type": result.last_event_type,
        }
        self._emit(
            on_event,
            ModelStreamEvent(
                "usage_completed",
                sequence_number=event_count,
                data={"usage": usage},
            ),
        )
        self._emit(
            on_event,
            ModelStreamEvent(
                "response_completed",
                sequence_number=event_count,
                data={
                    "response_id": result.response_id,
                    "status": result.status,
                    "tool_call_count": len(result.message.get("tool_calls") or []),
                },
            ),
        )
        return result

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        on_event: Optional[StreamCallback] = None,
    ) -> dict[str, Any]:
        """Compatibility entrypoint used by compactors and memory curators."""
        return self.respond(messages, tools=tools, on_event=on_event).message

    @classmethod
    def _to_responses_input(
        cls, messages: Iterable[dict[str, Any]]
    ) -> tuple[str, list[dict[str, Any]]]:
        instruction_parts: list[str] = []
        items: list[dict[str, Any]] = []
        for message in messages:
            role = str(message.get("role") or "")
            if role in {"system", "developer"}:
                content = cls._content_text(message.get("content"))
                if content:
                    instruction_parts.append(content)
                continue

            preserved = message.get("response_items")
            if isinstance(preserved, list) and preserved:
                items.extend(cls._clean_output_item(item) for item in preserved if isinstance(item, dict))
                continue

            if role == "tool":
                call_id = str(message.get("tool_call_id") or "")
                if call_id:
                    items.append(
                        {
                            "type": "function_call_output",
                            "call_id": call_id,
                            "output": cls._content_text(message.get("content")),
                        }
                    )
                continue

            if role == "assistant" and message.get("tool_calls"):
                content = cls._content_text(message.get("content"))
                if content:
                    items.append({"role": "assistant", "content": content})
                for call in message.get("tool_calls") or []:
                    function = call.get("function") or {}
                    items.append(
                        {
                            "type": "function_call",
                            "call_id": str(call.get("id") or ""),
                            "name": str(function.get("name") or ""),
                            "arguments": str(function.get("arguments") or "{}"),
                        }
                    )
                continue

            if role in {"user", "assistant"}:
                items.append({"role": role, "content": cls._content_text(message.get("content"))})
        return "\n\n".join(instruction_parts), items

    @staticmethod
    def _to_responses_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        converted: list[dict[str, Any]] = []
        for tool in tools:
            if tool.get("type") != "function":
                converted.append(dict(tool))
                continue
            function = tool.get("function") or tool
            converted.append(
                {
                    "type": "function",
                    "name": function.get("name"),
                    "description": function.get("description", ""),
                    "parameters": function.get("parameters") or {"type": "object", "properties": {}},
                    "strict": bool(function.get("strict", False)),
                }
            )
        return converted

    @classmethod
    def _legacy_message(cls, output_items: list[dict[str, Any]]) -> dict[str, Any]:
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        for item in output_items:
            item_type = item.get("type")
            if item_type == "message":
                for content in item.get("content") or []:
                    if not isinstance(content, dict):
                        continue
                    if content.get("type") in {"output_text", "text"}:
                        text_parts.append(str(content.get("text") or ""))
            elif item_type == "function_call":
                tool_calls.append(
                    {
                        "id": str(item.get("call_id") or item.get("id") or ""),
                        "type": "function",
                        "function": {
                            "name": str(item.get("name") or ""),
                            "arguments": str(item.get("arguments") or "{}"),
                        },
                    }
                )
        message: dict[str, Any] = {"role": "assistant", "content": "".join(text_parts)}
        if tool_calls:
            message["tool_calls"] = tool_calls
        return message

    @staticmethod
    def _clean_output_item(item: dict[str, Any]) -> dict[str, Any]:
        # Output items returned by the API are valid replay items. Drop only
        # explicit nulls so alternate OpenAI-compatible providers do not reject them.
        return {key: value for key, value in item.items() if value is not None}

    @staticmethod
    def _normalize_usage(usage: Any) -> dict[str, Any]:
        data = OpenAICompatibleClient._dump(usage)
        if not isinstance(data, dict):
            return {}
        input_tokens = data.get("input_tokens", data.get("prompt_tokens"))
        output_tokens = data.get("output_tokens", data.get("completion_tokens"))
        output_details = data.get("output_tokens_details") or {}
        reasoning_tokens = output_details.get("reasoning_tokens") if isinstance(output_details, dict) else None
        result = dict(data)
        result.update(
            {
                "prompt_tokens": input_tokens,
                "completion_tokens": output_tokens,
                "total_tokens": data.get("total_tokens"),
                "reasoning_tokens": reasoning_tokens,
            }
        )
        return result

    @staticmethod
    def _content_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    parts.append(str(item.get("text") or item.get("content") or ""))
            return "".join(parts)
        return "" if content is None else str(content)

    @staticmethod
    def _get(value: Any, key: str, default: Any = None) -> Any:
        if isinstance(value, dict):
            return value.get(key, default)
        return getattr(value, key, default)

    @staticmethod
    def _dump(value: Any) -> Any:
        if isinstance(value, (dict, list, str, int, float, bool)) or value is None:
            return value
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            return model_dump(mode="json", exclude_none=True)
        to_dict = getattr(value, "to_dict", None)
        if callable(to_dict):
            return to_dict()
        try:
            return json.loads(json.dumps(value, default=lambda item: vars(item)))
        except (TypeError, ValueError):
            return str(value)

    @staticmethod
    def _elapsed_ms(start: Optional[float], end: Optional[float]) -> Optional[float]:
        if start is None or end is None:
            return None
        return round(max(0.0, end - start) * 1000, 3)

    @staticmethod
    def _emit(callback: Optional[StreamCallback], event: ModelStreamEvent) -> None:
        if not callable(callback):
            return
        try:
            callback(event.to_dict())
        except Exception:
            # A UI callback must never interrupt reading the provider stream.
            pass
