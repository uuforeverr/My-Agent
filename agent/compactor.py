import json
from typing import Any, Callable, Optional, Union

LLMClient = Any
LLMProvider = Union[LLMClient, Callable[[], LLMClient]]


class CompactionError(RuntimeError):
    pass


def split_recent_turns(
    history: list[dict[str, Any]],
    keep_recent_turns: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    keep = max(1, int(keep_recent_turns))
    user_indices = [index for index, message in enumerate(history) if message.get("role") == "user"]
    if len(user_indices) <= keep:
        return [], list(history)
    cut = user_indices[-keep]
    return list(history[:cut]), list(history[cut:])


def build_summary_message(summary: str, compact_count: int, raw_path: str) -> dict[str, Any]:
    body = str(summary or "").strip()
    content = (
        f'<conversation_summary compact_count="{compact_count}">\n'
        f"{body}\n"
        f"原文：{raw_path}\n"
        "若摘要缺细节，用 memory_search 或按路径阅读原文。\n"
        "</conversation_summary>"
    )
    return {"role": "user", "content": content}


class Compactor:
    def __init__(self, llm: LLMProvider, prompt: str):
        self._llm = llm
        self.prompt = prompt

    def _client(self) -> LLMClient:
        llm = self._llm() if callable(self._llm) else self._llm
        if llm is None:
            raise CompactionError("compaction llm is not configured")
        return llm

    def summarize(
        self,
        messages: list[dict[str, Any]],
        compact_count: int,
        raw_path: str,
    ) -> str:
        payload = [
            {"role": "system", "content": self.prompt},
            {
                "role": "user",
                "content": self._render_user_prompt(messages, compact_count, raw_path),
            },
        ]
        reply = self._client().chat(payload)
        text = str((reply or {}).get("content") or "").strip()
        if not text:
            raise CompactionError("compaction llm returned an empty summary")
        return text

    @staticmethod
    def _render_user_prompt(
        messages: list[dict[str, Any]],
        compact_count: int,
        raw_path: str,
    ) -> str:
        rendered = json.dumps(messages, ensure_ascii=False, default=str)
        return (
            f"compact_count={compact_count}\n"
            f"transcript_path={raw_path}\n\n"
            "Compress the following older conversation turns into a short working summary.\n\n"
            f"{rendered}"
        )
