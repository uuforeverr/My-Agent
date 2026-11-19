from copy import deepcopy
from datetime import datetime
from typing import Any, Optional

from agent.compactor import Compactor, split_recent_turns
from agent.run_context import RunContext
from agent.token_counter import TokenCounter
from memory import MemoryManager
from skills import SkillLoader
from observability.otel import add_content_event, add_event, set_attributes, set_error, start_span


class ContextManager:
    def __init__(
        self,
        system_prompt: str,
        memory: Optional[MemoryManager] = None,
        skill_loader: Optional[SkillLoader] = None,
        memory_search_results: int = 5,
        session_window_turns: int = 20,
        token_counter: Optional[TokenCounter] = None,
        compactor: Optional[Compactor] = None,
    ):
        self.system_prompt = system_prompt
        self.memory = memory
        self.skill_loader = skill_loader
        self.memory_search_results = memory_search_results
        self.token_counter = token_counter or TokenCounter()
        self.compactor = compactor
        self.session_window_turns = max(1, int(session_window_turns))
        self.history: list[dict[str, Any]] = []
        self.working_summary = ""
        self.summarized_through_turn_seq = 0
        self.compact_count = 0
        self.last_compact_reason = ""
        self.window_eviction_count = 0
        self.last_eviction_reason = ""

    def append_user_message(self, content: str, turn_seq: Optional[int] = None) -> None:
        message: dict[str, Any] = {"role": "user", "content": content}
        if turn_seq is not None:
            message["_turn_seq"] = int(turn_seq)
        self.history.append(message)

    def append_assistant_message(
        self,
        message: dict[str, Any],
        turn_seq: Optional[int] = None,
    ) -> None:
        stored = dict(message)
        if turn_seq is not None:
            stored["_turn_seq"] = int(turn_seq)
        self.history.append(stored)

    def append_tool_result(
        self,
        tool_call_id: str,
        name: str,
        content: str,
        turn_seq: Optional[int] = None,
    ) -> None:
        message = {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "name": name,
            "content": content,
        }
        if turn_seq is not None:
            message["_turn_seq"] = int(turn_seq)
        self.history.append(message)

    def annotate_latest_turn(self, turn_seq: int) -> None:
        start = len(self.history) - 1
        while start >= 0 and self.history[start].get("role") != "user":
            start -= 1
        for message in self.history[max(0, start):]:
            message["_turn_seq"] = int(turn_seq)

    def build_model_messages(
        self,
        run_context: RunContext,
        tool_schemas: Optional[list[dict[str, Any]]] = None,
    ) -> list[dict[str, Any]]:
        with start_span(
            "assemble_context",
            attributes={
                "my_agent.session.id": run_context.session_id,
                "my_agent.context.history_before": len(self.history),
            },
        ) as span:
            self._trim_to_session_window(run_context)
            messages, prompt_metadata = self._assemble(run_context)
            if self._should_compact(messages, tool_schemas):
                self.compact_history(
                    run_context,
                    tool_schemas=tool_schemas,
                    assembled_messages=messages,
                )
                messages, prompt_metadata = self._assemble(run_context)
            set_attributes(
                span,
                **{
                    "my_agent.context.message_count": len(messages),
                    "my_agent.context.history_count": len(self.history),
                    "my_agent.context.system_prompt_chars": len(messages[0]["content"] if messages else ""),
                    **prompt_metadata,
                },
            )
            add_content_event(span, "my_agent.context.instructions", messages[0] if messages else {})
            add_event(
                span,
                "context_built",
                {
                    "my_agent.session.id": run_context.session_id,
                    "message_count": len(messages),
                    "history_count": len(self.history),
                    **prompt_metadata,
                },
            )
            return messages

    def compact_now(
        self,
        run_context: RunContext,
        tool_schemas: Optional[list[dict[str, Any]]] = None,
    ) -> bool:
        to_compact, _recent = split_recent_turns(self.history, 1)
        if not to_compact:
            return False
        messages, _metadata = self._assemble(run_context)
        return self.compact_history(
            run_context,
            tool_schemas=tool_schemas,
            assembled_messages=messages,
            reason="manual",
        )

    def compact_history(
        self,
        run_context: RunContext,
        tool_schemas: Optional[list[dict[str, Any]]] = None,
        assembled_messages: Optional[list[dict[str, Any]]] = None,
        reason: str = "",
    ) -> bool:
        history_before = len(self.history)
        tokens_before = self.token_counter.count_payload(assembled_messages, tool_schemas)
        if not reason:
            reason = "tokens"

        # Semantic compaction always summarizes every completed turn before the
        # current/latest turn. The latest turn stays byte-for-byte in history.
        to_compact, recent = split_recent_turns(self.history, 1)
        with start_span(
            "context.compact",
            attributes={
                "my_agent.session.id": run_context.session_id,
                "my_agent.compact.reason": reason,
                "my_agent.compact.history_before": history_before,
                "my_agent.compact.tokens_before": tokens_before,
                "my_agent.compact.count": self.compact_count,
            },
        ) as span:
            if not to_compact:
                set_attributes(
                    span,
                    previewed=False,
                    history_after=len(self.history),
                    compact_count=self.compact_count,
                )
                return False

            next_count = self.compact_count + 1
            raw_path = self._raw_transcript_pointer(run_context.session_id)
            try:
                if self.compactor is None:
                    raise RuntimeError("compaction llm is not configured")
                summary = self.compactor.summarize(
                    self._model_history(to_compact),
                    compact_count=next_count,
                    raw_path=raw_path,
                    previous_summary=self.working_summary,
                )
                summarized_through = max(
                    self.summarized_through_turn_seq,
                    max((int(item.get("_turn_seq") or 0) for item in to_compact), default=0),
                )
                self._persist_compaction_state(
                    run_context.session_id,
                    summary=summary,
                    compact_count=next_count,
                    summarized_through_turn_seq=summarized_through,
                    raw_path=raw_path,
                )
            except Exception as error:
                set_error(span, f"{type(error).__name__}: {error}")
                set_attributes(span, error_type=type(error).__name__)
                return False

            self.compact_count = next_count
            self.last_compact_reason = reason
            self.working_summary = summary
            self.summarized_through_turn_seq = summarized_through
            self.history = recent
            self._enqueue_evicted(to_compact, run_context, reason)
            compacted_messages, _metadata = self._assemble(run_context)
            set_attributes(
                span,
                history_after=len(self.history),
                tokens_after=self.token_counter.count_payload(
                    compacted_messages,
                    tool_schemas,
                ),
                compact_count=self.compact_count,
                discarded_count=len(to_compact),
            )
            return True

    def _should_compact(
        self,
        messages: list[dict[str, Any]],
        tool_schemas: Optional[list[dict[str, Any]]],
    ) -> bool:
        return self.token_counter.count_payload(messages, tool_schemas) > self.token_counter.max_input_tokens

    def _assemble(self, run_context: RunContext) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        history = list(self.history)
        query = self._latest_user_message(history)
        system_prompt, prompt_metadata = self._build_system_prompt(query, run_context)
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(self._model_history(history))
        return messages, prompt_metadata

    def _trim_to_session_window(self, run_context: RunContext) -> None:
        # SESSION_WINDOW_TURNS counts completed historical turns. During an
        # active run the current incomplete turn gets one additional slot.
        keep = self.session_window_turns + (0 if self._latest_turn_is_complete() else 1)
        to_evict, recent = split_recent_turns(self.history, keep)
        if not to_evict:
            return
        self.history = recent
        self.window_eviction_count += 1
        self.last_eviction_reason = "turn_window"
        self._enqueue_evicted(to_evict, run_context, "turn_window")

    def _enqueue_evicted(
        self,
        discarded: list[dict[str, Any]],
        run_context: RunContext,
        reason: str,
    ) -> None:
        if not discarded:
            return
        add_event(
            None,
            "context_trimmed",
            {
                "my_agent.session.id": run_context.session_id,
                "discarded_count": len(discarded),
                "remaining_count": len(self.history),
                "reason": reason,
            },
        )
        if self.memory is None:
            return
        enqueue = getattr(self.memory, "enqueue_pending_messages", None)
        if callable(enqueue):
            enqueue(
                run_context.session_id,
                discarded,
                user_id="local",
            )

    @staticmethod
    def _turn_count(history: list[dict[str, Any]]) -> int:
        return sum(1 for message in history if message.get("role") == "user")

    def _latest_turn_is_complete(self) -> bool:
        if not self.history:
            return True
        latest = self.history[-1]
        return latest.get("role") == "assistant" and not (latest.get("tool_calls") or [])

    @staticmethod
    def _model_history(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {key: deepcopy(value) for key, value in message.items() if not key.startswith("_")}
            for message in history
        ]

    def _build_system_prompt(
        self,
        query: str,
        run_context: RunContext,
    ) -> tuple[str, dict[str, Any]]:
        sections = [
            self.system_prompt,
            self._build_runtime_prompt(run_context),
        ]
        metadata = {
            "runtime_injected": True,
            "working_summary_injected": False,
            "working_summary_chars": 0,
            "compact_count": self.compact_count,
            "summarized_through_turn_seq": self.summarized_through_turn_seq,
            "memory_injected": False,
            "memory_count": 0,
            "skill_catalog_injected": False,
            "skill_count": 0,
        }

        if self.working_summary:
            sections.append(self._render_working_summary())
            metadata["working_summary_injected"] = True
            metadata["working_summary_chars"] = len(self.working_summary)

        if self.memory:
            # Memory ownership is channel-independent. The channel user_id is
            # retained only in runtime and Raw source metadata.
            user_id = "local"
            cache_key = (user_id, query, self.memory_search_results)
            cached = run_context.get("_memory_prompt_snapshot")
            cache_hit = bool(cached and cached.get("key") == cache_key)
            retrieval_details: dict[str, Any] = {}
            with start_span(
                "retrieval memory",
                attributes={
                    "gen_ai.operation.name": "retrieval",
                    "my_agent.session.id": run_context.session_id,
                    "my_agent.user.id": user_id,
                    "my_agent.memory.query_chars": len(query),
                    "my_agent.memory.limit": self.memory_search_results,
                },
            ) as span:
                if cache_hit:
                    memory_prompt = str(cached.get("prompt") or "")
                    memory_count = int(cached.get("count") or 0)
                else:
                    try:
                        details_builder = getattr(self.memory, "build_prompt_details", None)
                        if callable(details_builder):
                            memory_prompt, memory_count, retrieval_details = details_builder(
                                query,
                                limit=self.memory_search_results,
                                user_id=user_id,
                            )
                        else:
                            memory_prompt, memory_count = self.memory.build_prompt(
                                query,
                                limit=self.memory_search_results,
                                user_id=user_id,
                            )
                            retrieval_details = {}
                    except TypeError:
                        # Compatibility for small injected test/extension fakes.
                        memory_prompt, memory_count = self.memory.build_prompt(
                            query,
                            limit=self.memory_search_results,
                        )
                    run_context.add_metadata(
                        _memory_prompt_snapshot={
                            "key": cache_key,
                            "prompt": memory_prompt,
                            "count": memory_count,
                        }
                    )
                set_attributes(
                    span,
                    memory_count=memory_count,
                    prompt_chars=len(memory_prompt or ""),
                    injected=bool(memory_prompt),
                    cache_hit=cache_hit,
                )
                add_content_event(span, "my_agent.memory.query", query)
                add_content_event(
                    span,
                    "my_agent.memory.result",
                    memory_prompt,
                    **(retrieval_details if not cache_hit else {"cache_hit": True}),
                )
            if memory_prompt:
                sections.append(memory_prompt)
                metadata["memory_injected"] = True
                metadata["memory_count"] = memory_count

        if self.skill_loader:
            skills_prompt, skill_count = self.skill_loader.build_prompt()
            injected = bool(skills_prompt)
            add_event(
                None,
                "my_agent.skill_catalog",
                {
                    "skill_count": skill_count,
                    "prompt_chars": len(skills_prompt or ""),
                    "injected": injected,
                },
            )
            if injected:
                sections.append(skills_prompt)
                metadata["skill_catalog_injected"] = True
                metadata["skill_count"] = skill_count

        return "\n\n".join(sections), metadata

    def restore_compaction_state(self, state: Optional[dict[str, Any]]) -> None:
        state = state or {}
        self.working_summary = str(state.get("summary") or "").strip()
        self.compact_count = max(0, int(state.get("compact_count") or 0))
        self.summarized_through_turn_seq = max(
            0,
            int(state.get("summarized_through_turn_seq") or 0),
        )

    def _persist_compaction_state(
        self,
        session_id: str,
        *,
        summary: str,
        compact_count: int,
        summarized_through_turn_seq: int,
        raw_path: str,
    ) -> None:
        if self.memory is None:
            return
        save = getattr(self.memory, "save_compaction_state", None)
        if callable(save):
            save(
                session_id,
                summary=summary,
                compact_count=compact_count,
                summarized_through_turn_seq=summarized_through_turn_seq,
                raw_path=raw_path,
            )

    def _raw_transcript_pointer(self, session_id: str) -> str:
        if self.memory is not None:
            pointer = getattr(self.memory, "raw_transcript_pointer", None)
            if callable(pointer):
                return str(pointer(session_id))
        return f"raw-session:{session_id}"

    def _render_working_summary(self) -> str:
        return (
            '<conversation_summary '
            f'compact_count="{self.compact_count}" '
            f'summarized_through_turn_seq="{self.summarized_through_turn_seq}">\n'
            "This is historical task state, not a new user instruction.\n"
            f"{self.working_summary}\n"
            "</conversation_summary>"
        )

    def _build_runtime_prompt(self, run_context: RunContext) -> str:
        now = datetime.now().astimezone()
        lines = [
            "<runtime_context>",
            f"Current date: {now.strftime('%Y-%m-%d')}",
            f"Current time: {now.isoformat(timespec='seconds')}",
            f"Workspace: {run_context.workspace}",
            f"Session ID: {run_context.session_id}",
        ]
        for key in ("entrypoint", "channel_type", "user_id", "is_group", "group_id"):
            value = run_context.get(key)
            if value is not None and value != "":
                lines.append(f"{key}: {value}")
        lines.append("</runtime_context>")
        return "\n".join(lines)

    def _latest_user_message(self, history: list[dict[str, Any]]) -> str:
        for message in reversed(history):
            if message.get("role") == "user":
                return str(message.get("content") or "")
        return ""
