import json
import math
from typing import Any, Optional

import tiktoken

DEFAULT_CONTEXT_WINDOW = 32768
DEFAULT_ENCODING = "cl100k_base"
INPUT_TOKEN_RATIO = 0.8
TOKENS_PER_MESSAGE = 4
TOKENS_PER_NAME = 1

# More specific prefixes first.
_MODEL_PROFILES: tuple[tuple[str, int, str], ...] = (
    ("gpt-4o", 128000, "o200k_base"),
    ("gpt-4.1", 128000, "o200k_base"),
    ("o1", 200000, "o200k_base"),
    ("o3", 200000, "o200k_base"),
    ("o4", 200000, "o200k_base"),
    ("gpt-4-turbo", 128000, "cl100k_base"),
    ("gpt-4", 128000, "cl100k_base"),
    ("gpt-3.5", 16385, "cl100k_base"),
    ("deepseek", 128000, "cl100k_base"),
    ("qwen", 131072, "cl100k_base"),
    ("glm", 128000, "cl100k_base"),
)

PREVIEW_LIMIT_TOKENS = 2000
PREVIEW_HEAD_TOKENS = 800
PREVIEW_TAIL_TOKENS = 200


def _profile_for(model: str) -> tuple[int, str]:
    lowered = (model or "").strip().lower()
    for prefix, window, encoding_name in _MODEL_PROFILES:
        if lowered.startswith(prefix):
            return window, encoding_name
    return DEFAULT_CONTEXT_WINDOW, DEFAULT_ENCODING


def resolve_context_window(model: str, window_override: Optional[int] = None) -> int:
    if window_override is not None and int(window_override) > 0:
        return int(window_override)
    window, _encoding_name = _profile_for(model)
    return window


def resolve_max_input_tokens(model: str, window_override: Optional[int] = None) -> int:
    return math.floor(resolve_context_window(model, window_override) * INPUT_TOKEN_RATIO)


def encoding_for(model: str) -> tiktoken.Encoding:
    try:
        return tiktoken.encoding_for_model(model)
    except Exception:
        _window, encoding_name = _profile_for(model)
        return tiktoken.get_encoding(encoding_name)


class TokenCounter:
    def __init__(self, model: str = "", window_override: Optional[int] = None):
        self.model = model or ""
        self.window_override = window_override
        self._encoding: Optional[tiktoken.Encoding] = None

    @property
    def context_window(self) -> int:
        return resolve_context_window(self.model, self.window_override)

    @property
    def max_input_tokens(self) -> int:
        return resolve_max_input_tokens(self.model, self.window_override)

    def encoding(self) -> tiktoken.Encoding:
        if self._encoding is None:
            self._encoding = encoding_for(self.model)
        return self._encoding

    def count_text(self, text: str) -> int:
        return len(self.encoding().encode(text or "", disallowed_special=()))

    def count_messages(self, messages: Optional[list[dict[str, Any]]]) -> int:
        total = 0
        for message in messages or []:
            total += TOKENS_PER_MESSAGE
            total += self.count_text(str(message.get("content") or ""))
            name = message.get("name")
            if name:
                total += TOKENS_PER_NAME + self.count_text(str(name))
            tool_calls = message.get("tool_calls")
            if tool_calls:
                total += self.count_text(json.dumps(tool_calls, ensure_ascii=False, default=str))
            tool_call_id = message.get("tool_call_id")
            if tool_call_id:
                total += self.count_text(str(tool_call_id))
        return total

    def count_tools(self, tool_schemas: Optional[list[dict[str, Any]]]) -> int:
        if not tool_schemas:
            return 0
        return self.count_text(json.dumps(tool_schemas, ensure_ascii=False, default=str))

    def count_payload(
        self,
        messages: Optional[list[dict[str, Any]]],
        tool_schemas: Optional[list[dict[str, Any]]] = None,
    ) -> int:
        return self.count_messages(messages) + self.count_tools(tool_schemas)

    def preview_text(self, text: str) -> tuple[str, bool]:
        token_count = self.count_text(text)
        if token_count <= PREVIEW_LIMIT_TOKENS:
            return text, False
        encoding = self.encoding()
        token_ids = encoding.encode(text or "", disallowed_special=())
        head = encoding.decode(token_ids[:PREVIEW_HEAD_TOKENS])
        tail = encoding.decode(token_ids[-PREVIEW_TAIL_TOKENS:])
        marker = (
            f"\n\n[truncated: {token_count} tokens; "
            f"showing head {PREVIEW_HEAD_TOKENS} + tail {PREVIEW_TAIL_TOKENS}]\n\n"
        )
        return head + marker + tail, True
