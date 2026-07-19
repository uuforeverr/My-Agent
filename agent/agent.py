import threading
import time
from typing import Any, Callable, Optional

from agent.context_manager import ContextManager
from agent.run_context import RunContext
from config import Settings
from llm.openai_client import OpenAICompatibleClient
from memory import MemoryManager
from observability import Trace
from prompts import PromptLoader
from scheduler.task_store import TaskStore
from skills import SkillLoader
from tools.registry import ToolRegistry


class Agent:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.llm = OpenAICompatibleClient(settings)
        self.prompt_loader = PromptLoader(settings.project_root / "prompts" / "identity.md")
        self.memory = MemoryManager(settings.project_root / "data" / "memory", llm=self.llm)
        self.skill_loader = SkillLoader(settings.project_root / "skills")
        self.scheduler_store = (
            TaskStore(settings.scheduler_tasks_path) if settings.scheduler_enabled else None
        )
        self.tools = ToolRegistry(
            settings.workspace,
            self.memory,
            bash_timeout=settings.bash_timeout,
            qianfan_api_key=settings.qianfan_api_key,
            qianfan_api_base=settings.qianfan_api_base,
            web_search_timeout=settings.web_search_timeout,
            scheduler_store=self.scheduler_store,
        )
        self.identity_prompt = self.prompt_loader.load_identity()
        self._context_managers: dict[str, ContextManager] = {}
        self._context_managers_lock = threading.Lock()

    def _get_context_manager(self, session_id: str) -> ContextManager:
        with self._context_managers_lock:
            manager = self._context_managers.get(session_id)
            if manager is None:
                manager = ContextManager(
                    self.identity_prompt,
                    max_history_messages=self.settings.max_history_messages,
                    memory=self.memory,
                    skill_loader=self.skill_loader,
                    memory_search_results=self.settings.memory_search_results,
                    memory_flush_messages=self.settings.memory_flush_messages,
                )
                self._context_managers[session_id] = manager
            return manager

    def run(
        self,
        user_message: str,
        context: Optional[RunContext] = None,
        on_event: Optional[Callable[[dict[str, Any]], None]] = None,
    ) -> tuple[str, Trace]:
        context = context or RunContext.create(
            session_id="default",
            workspace=self.settings.workspace,
        )
        trace = context.trace
        with trace.start_span(
            "agent.invoke",
            span_kind="agent.invoke",
            session_id=context.session_id,
            entrypoint=context.get("entrypoint"),
            channel_type=context.get("channel_type"),
            user_message_chars=len(user_message),
        ) as agent_span:
            context_manager = self._get_context_manager(context.session_id)
            context_manager.append_user_message(user_message)
            self.memory.record_message(
                session_id=context.session_id,
                role="user",
                content=user_message,
                metadata={"entrypoint": context.get("entrypoint")},
            )
            trace.add(
                "user_message",
                chars=len(user_message),
                session_id=context.session_id,
            )

            for step in range(1, self.settings.max_steps + 1):
                model_messages = context_manager.build_model_messages(context)
                tool_schemas = self.tools.schemas(context)
                llm_start = time.monotonic()
                with trace.start_span(
                    "llm.chat",
                    span_kind="llm.chat",
                    step=step,
                    session_id=context.session_id,
                    model=self.settings.model,
                    message_count=len(model_messages),
                    tool_schema_count=len(tool_schemas),
                ) as llm_span:
                    message = self.llm.chat(model_messages, tools=tool_schemas)
                    tool_calls = message.get("tool_calls") or []
                    response_metadata = getattr(self.llm, "last_response_metadata", {}) or {}
                    usage = response_metadata.get("usage") or {}
                    llm_span.set_attributes(
                        response_model=response_metadata.get("model") or self.settings.model,
                        response_content_chars=len(message.get("content") or ""),
                        tool_call_count=len(tool_calls),
                        finish_reason=response_metadata.get("finish_reason") or ("tool_calls" if tool_calls else "stop"),
                        input_tokens=usage.get("prompt_tokens"),
                        output_tokens=usage.get("completion_tokens"),
                        total_tokens=usage.get("total_tokens"),
                    )

                trace.add(
                    "llm_call",
                    step=step,
                    session_id=context.session_id,
                    duration_ms=round((time.monotonic() - llm_start) * 1000, 2),
                )

                context_manager.append_assistant_message(message)
                self.memory.record_message(
                    session_id=context.session_id,
                    role="assistant",
                    content=message.get("content") or "",
                    metadata={"tool_calls": message.get("tool_calls") or []},
                )

                if not tool_calls:
                    answer = message.get("content") or ""
                    agent_span.set_attributes(
                        final_answer_chars=len(answer),
                        steps=step,
                        stopped_reason="final_answer",
                    )
                    trace.add("final_answer", total_ms=trace.total_ms())
                    return answer, trace

                progress = (message.get("content") or "").strip()
                if progress:
                    self._emit_event(
                        on_event,
                        {
                            "type": "assistant_progress",
                            "content": progress,
                            "step": step,
                            "tool_calls": tool_calls,
                        },
                    )

                for call in tool_calls:
                    function = call.get("function") or {}
                    tool_name = function.get("name", "")
                    arguments = function.get("arguments", "{}")

                    tool_start = time.monotonic()
                    result = self.tools.run(tool_name, arguments, context)
                    trace.add(
                        "tool_call",
                        step=step,
                        session_id=context.session_id,
                        tool_name=tool_name,
                        status="success" if result.ok else "error",
                        duration_ms=round((time.monotonic() - tool_start) * 1000, 2),
                    )

                    context_manager.append_tool_result(
                        tool_call_id=call.get("id"),
                        name=tool_name,
                        content=result.content,
                    )
                    self.memory.record_message(
                        session_id=context.session_id,
                        role="tool",
                        content=result.content,
                        name=tool_name,
                        metadata={
                            "tool_call_id": call.get("id"),
                            "ok": result.ok,
                        },
                    )

            fallback = f"Reached max_steps={self.settings.max_steps}; stopping to avoid an infinite tool loop."
            agent_span.set_attributes(
                final_answer_chars=len(fallback),
                steps=self.settings.max_steps,
                stopped_reason="max_steps_reached",
            )
            trace.add("max_steps_reached", total_ms=trace.total_ms())
            context_manager.append_assistant_message({"role": "assistant", "content": fallback})
            self.memory.record_message(
                session_id=context.session_id,
                role="assistant",
                content=fallback,
                metadata={"reason": "max_steps_reached"},
            )
            return fallback, trace

    @staticmethod
    def _emit_event(
        on_event: Optional[Callable[[dict[str, Any]], None]],
        event: dict[str, Any],
    ) -> None:
        if on_event is None:
            return
        try:
            on_event(event)
        except Exception:
            pass
