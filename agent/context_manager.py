from copy import deepcopy
from datetime import datetime
from typing import Any, Optional

from agent.run_context import RunContext
from memory import MemoryManager
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
    ):
        self.system_prompt = system_prompt
        self.max_history_messages = max_history_messages
        self.memory = memory
        self.skill_loader = skill_loader
        self.memory_search_results = memory_search_results
        self.memory_flush_messages = memory_flush_messages
        self.history: list[dict[str, Any]] = []

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

    def build_model_messages(self, run_context: RunContext) -> list[dict[str, Any]]:
        with run_context.trace.start_span(
            "context.build",
            span_kind="context.build",
            session_id=run_context.session_id,
            history_before=len(self.history),
        ) as span:
            history = self._trim_history(run_context)
            query = self._latest_user_message(history)
            system_prompt, prompt_metadata = self._build_system_prompt(query, run_context)
            messages = [{"role": "system", "content": system_prompt}]
            messages.extend(deepcopy(history))
            span.set_attributes(
                message_count=len(messages),
                history_count=len(history),
                system_prompt_chars=len(system_prompt),
                **prompt_metadata,
            )
            run_context.trace.add(
                "context_built",
                session_id=run_context.session_id,
                message_count=len(messages),
                history_count=len(history),
                **prompt_metadata,
            )
            return messages

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

    def _trim_history(self, run_context: RunContext) -> list[dict[str, Any]]:
        if len(self.history) <= self.max_history_messages:
            return list(self.history)

        discard_count = len(self.history) - self.max_history_messages
        discarded = list(self.history[:discard_count])
        trimmed = list(self.history[discard_count:])

        # A leading tool message is invalid without the assistant tool_call that
        # requested it. Drop leading tool messages after simple count-based trim.
        while trimmed and trimmed[0].get("role") == "tool":
            discarded.append(trimmed.pop(0))

        self.history = trimmed
        run_context.trace.add(
            "context_trimmed",
            session_id=run_context.session_id,
            discarded_count=len(discarded),
            remaining_count=len(trimmed),
        )

        if self.memory and discarded:
            with run_context.trace.start_span(
                "memory.flush",
                span_kind="memory.flush",
                session_id=run_context.session_id,
                reason="context_trim",
                discarded_count=len(discarded),
                max_messages=self.memory_flush_messages,
            ) as span:
                flush_stats = self.memory.flush_from_messages(
                    discarded,
                    reason="context_trim",
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

        return trimmed

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
