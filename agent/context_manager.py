from copy import deepcopy
from datetime import datetime
from typing import Any, Optional

from agent.compactor import Compactor, build_summary_message, split_recent_turns
from agent.run_context import RunContext
from agent.token_counter import TokenCounter
from memory import MemoryManager
from memory.raw_store import RawConversationStore
from skills import SkillLoader


class ContextManager:
    def __init__(
        self,
        system_prompt: str,
        max_history_messages: int = 40,
        memory: Optional[MemoryManager] = None,
        skill_loader: Optional[SkillLoader] = None,
        memory_search_results: int = 5,
        memory_flush_messages: int = 12,
        keep_recent_turns: int = 3,
        token_counter: Optional[TokenCounter] = None,
        compactor: Optional[Compactor] = None,
    ):
        self.system_prompt = system_prompt
        self.max_history_messages = max_history_messages
        self.memory = memory
        self.skill_loader = skill_loader
        self.memory_search_results = memory_search_results
        self.memory_flush_messages = memory_flush_messages
        self.keep_recent_turns = max(1, int(keep_recent_turns))
        self.token_counter = token_counter or TokenCounter()
        self.compactor = compactor
        self.history: list[dict[str, Any]] = []
        self.compact_count = 0
        self.last_compact_reason = ""

    def append_user_message(self, content: str) -> None:
        self.history.append({"role": "user", "content": content})

    def append_assistant_message(self, message: dict[str, Any]) -> None:
        self.history.append(message)

    def append_tool_result(self, tool_call_id: str, name: str, content: str) -> None:
        self.history.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "name": name,
            "content": content,
        })

    def build_model_messages(
        self,
        run_context: RunContext,
        tool_schemas: Optional[list[dict[str, Any]]] = None,
    ) -> list[dict[str, Any]]:
        with run_context.trace.start_span(
            "context.build",
            span_kind="context.build",
            session_id=run_context.session_id,
            history_before=len(self.history),
        ) as span:
            messages, prompt_metadata = self._assemble(run_context)
            if self._should_compact(messages, tool_schemas):
                self.compact_history(
                    run_context,
                    tool_schemas=tool_schemas,
                    assembled_messages=messages,
                )
                messages, prompt_metadata = self._assemble(run_context)
            span.set_attributes(
                message_count=len(messages),
                history_count=len(self.history),
                system_prompt_chars=len(messages[0]["content"] if messages else ""),
                **prompt_metadata,
            )
            run_context.trace.add(
                "context_built",
                session_id=run_context.session_id,
                message_count=len(messages),
                history_count=len(self.history),
                **prompt_metadata,
            )
            return messages

    def compact_now(
        self,
        run_context: RunContext,
        tool_schemas: Optional[list[dict[str, Any]]] = None,
    ) -> bool:
        to_compact, _recent = split_recent_turns(self.history, self.keep_recent_turns)
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
        count_over = history_before > self.max_history_messages
        tokens_over = tokens_before > self.token_counter.max_input_tokens
        if not reason:
            reason = "count" if count_over else "tokens"

        to_compact, recent = split_recent_turns(self.history, self.keep_recent_turns)
        with run_context.trace.start_span(
            "context.compact",
            span_kind="context.compact",
            session_id=run_context.session_id,
            reason=reason,
            history_before=history_before,
            tokens_before=tokens_before,
            compact_count=self.compact_count,
        ) as span:
            if not to_compact:
                span.set_attributes(
                    previewed=False,
                    history_after=len(self.history),
                    compact_count=self.compact_count,
                )
                return False

            try:
                if self.compactor is None:
                    raise RuntimeError("compaction llm is not configured")
                next_count = self.compact_count + 1
                raw_path = self._raw_transcript_pointer(run_context.session_id)
                summary = self.compactor.summarize(
                    to_compact,
                    compact_count=next_count,
                    raw_path=raw_path,
                )
            except Exception as error:
                span.record_error(error)
                span.set_attribute("error_type", type(error).__name__)
                return False

            self.compact_count = next_count
            self.last_compact_reason = reason
            self.history = [build_summary_message(summary, next_count, raw_path)] + recent
            self._flush_discarded(to_compact, run_context, reason="context_compact")
            span.set_attributes(
                history_after=len(self.history),
                tokens_after=self.token_counter.count_payload(
                    [{"role": "system", "content": ""}] + self.history,
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
        if len(self.history) > self.max_history_messages:
            return True
        return self.token_counter.count_payload(messages, tool_schemas) > self.token_counter.max_input_tokens

    def _assemble(self, run_context: RunContext) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        history = list(self.history)
        query = self._latest_user_message(history)
        system_prompt, prompt_metadata = self._build_system_prompt(query, run_context)
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(deepcopy(history))
        return messages, prompt_metadata

    def _flush_discarded(
        self,
        discarded: list[dict[str, Any]],
        run_context: RunContext,
        reason: str,
    ) -> None:
        if not discarded:
            return
        run_context.trace.add(
            "context_trimmed",
            session_id=run_context.session_id,
            discarded_count=len(discarded),
            remaining_count=len(self.history),
            reason=reason,
        )
        if not self.memory:
            return
        with run_context.trace.start_span(
            "memory.flush",
            span_kind="memory.flush",
            session_id=run_context.session_id,
            reason=reason,
            discarded_count=len(discarded),
            max_messages=self.memory_flush_messages,
        ) as span:
            flush_stats = self.memory.flush_from_messages(
                discarded,
                reason=reason,
                session_id=run_context.session_id,
                max_messages=self.memory_flush_messages,
            )
            span.set_attributes(
                async_dispatched=bool(flush_stats.get("async")),
                skipped=bool(flush_stats.get("skipped")),
                ok=bool(flush_stats.get("ok")),
            )
        future = flush_stats.get("future")
        if hasattr(future, "add_done_callback"):
            future.add_done_callback(
                lambda done: self._record_memory_flush_result(run_context, done)
            )
        trace_stats = {k: v for k, v in flush_stats.items() if k != "future"}
        run_context.trace.add(
            "memory_flush_dispatched",
            session_id=run_context.session_id,
            **trace_stats,
        )

    def _raw_transcript_pointer(self, session_id: str) -> str:
        safe_session = RawConversationStore._safe_session_id(session_id)
        day = datetime.now().strftime("%Y-%m-%d")
        return f"data/memory/raw/{safe_session}/{day}.jsonl"

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
            "memory_injected": False,
            "memory_count": 0,
            "skill_catalog_injected": False,
            "skill_count": 0,
        }

        if self.memory:
            with run_context.trace.start_span(
                "memory.inject",
                span_kind="memory.inject",
                session_id=run_context.session_id,
                query_chars=len(query),
                limit=self.memory_search_results,
            ) as span:
                memory_prompt, memory_count = self.memory.build_prompt(
                    query,
                    limit=self.memory_search_results,
                )
                span.set_attributes(
                    memory_count=memory_count,
                    prompt_chars=len(memory_prompt or ""),
                    injected=bool(memory_prompt),
                )
            if memory_prompt:
                sections.append(memory_prompt)
                metadata["memory_injected"] = True
                metadata["memory_count"] = memory_count

        if self.skill_loader:
            with run_context.trace.start_span(
                "skill.catalog.load",
                span_kind="skill.catalog.load",
                session_id=run_context.session_id,
            ) as span:
                skills_prompt, skill_count = self.skill_loader.build_prompt()
                span.set_attributes(
                    skill_count=skill_count,
                    prompt_chars=len(skills_prompt or ""),
                )

            with run_context.trace.start_span(
                "skill.catalog.inject",
                span_kind="skill.catalog.inject",
                session_id=run_context.session_id,
                skill_count=skill_count,
                prompt_chars=len(skills_prompt or ""),
            ) as span:
                injected = bool(skills_prompt)
                span.set_attribute("injected", injected)
                if injected:
                    sections.append(skills_prompt)
                    metadata["skill_catalog_injected"] = True
                    metadata["skill_count"] = skill_count

        return "\n\n".join(sections), metadata

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

    @staticmethod
    def _record_memory_flush_result(run_context: RunContext, future: Any) -> None:
        try:
            result = future.result()
        except Exception as e:
            result = {
                "ok": False,
                "skipped": False,
                "error": f"{type(e).__name__}: {e}",
            }
        run_context.trace.add(
            "memory_flush_completed",
            session_id=run_context.session_id,
            **result,
        )

    def _latest_user_message(self, history: list[dict[str, Any]]) -> str:
        for message in reversed(history):
            if message.get("role") == "user":
                return str(message.get("content") or "")
        return ""
