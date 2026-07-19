import json
import urllib.error
import urllib.request
from typing import Any, Optional

from config import Settings


class LLMError(RuntimeError):
    pass


class OpenAICompatibleClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.last_response_metadata: dict[str, Any] = {}

    def chat(self, messages: list[dict[str, Any]], tools: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
        if not self.settings.openai_api_key:
            raise LLMError("OPENAI_API_KEY is not set. Create .env from .env.example first.")

        payload: dict[str, Any] = {
            "model": self.settings.model,
            "messages": messages,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        req = urllib.request.Request(
            url=f"{self.settings.openai_api_base}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.settings.openai_api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise LLMError(f"LLM HTTP {e.code}: {body}") from e
        except Exception as e:
            raise LLMError(f"LLM request failed: {e}") from e

        try:
            choice = data["choices"][0]
            self.last_response_metadata = {
                "model": data.get("model"),
                "usage": data.get("usage") or {},
                "finish_reason": choice.get("finish_reason"),
            }
            return choice["message"]
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"Unexpected LLM response: {data}") from e
