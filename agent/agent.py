import threading
import time
from typing import Any, Callable, Optional

from agent.compactor import Compactor
from agent.context_manager import ContextManager
from agent.run_context import RunContext
from agent.token_counter import TokenCounter
from config import Settings
from llm.openai_client import OpenAICompatibleClient
from memory import MemoryManager
from observability import Trace
from prompts import PromptLoader
from scheduler.task_store import TaskStore
from skills import SkillLoader
from tools.approval import ApprovalGate
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
            approval_mode=settings.tool_approval_mode,
            approval_gate=ApprovalGate(),
        )
        self.identity_prompt = self.prompt_loader.load_identity()
        self.compaction_prompt = self._load_compaction_prompt()
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
                    keep_recent_turns=self.settings.keep_recent_turns,
                    token_counter=TokenCounter(
                        model=self.settings.model,
                        window_override=self.settings.model_context_window,
                    ),
                    compactor=Compactor(lambda: self.llm, self.compaction_prompt),
                )
                self._context_managers[session_id] = manager
                if not manager.history:
                    manager.history = self._hydrate_history(session_id)
            return manager

    def _hydrate_history(self, session_id: str) -> list[dict[str, Any]]:
        records = self.memory.raw_store.read_session(session_id)
        messages: list[dict[str, Any]] = []
        for record in records:
            message = self._raw_record_to_history_message(record)
            if message is not None:
                messages.append(message)
        return messages

    @staticmethod
    def _raw_record_to_history_message(record: dict[str, Any]) -> Optional[dict[str, Any]]:
        role = record.get("role")
        if role not in {"user", "assistant", "tool"}:
            return None
        message: dict[str, Any] = {
            "role": role,
            "content": record.get("content") or "",
        }
        metadata = record.get("metadata") or {}
        if role == "assistant":
            tool_calls = metadata.get("tool_calls")
            if tool_calls:
                message["tool_calls"] = tool_calls
        elif role == "tool":
            name = record.get("name") or metadata.get("name")
            if name:
                message["name"] = name
            tool_call_id = metadata.get("tool_call_id")
            if tool_call_id:
                message["tool_call_id"] = tool_call_id
        return message

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
        if on_event is not None:
            context.add_metadata(on_event=on_event)
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
                tool_schemas = self.tools.schemas(context)
                model_messages = context_manager.build_model_messages(
                    context,
                    tool_schemas=tool_schemas,
                )
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

                for index, call in enumerate(tool_calls):
                    function = call.get("function") or {}
                    tool_name = function.get("name", "")
                    arguments = function.get("arguments", "{}")

                    tool_start = time.monotonic()
                    result = self.tools.run(tool_name, arguments, context)
                    parked = bool(getattr(result, "waiting_approval", False))
                    trace.add(
                        "tool_call",
                        step=step,
                        session_id=context.session_id,
                        tool_name=tool_name,
                        status="waiting_approval" if parked else ("success" if result.ok else "error"),
                        duration_ms=round((time.monotonic() - tool_start) * 1000, 2),
                    )
                    if parked:
                        self._park_scheduled_call(
                            context,
                            result,
                            call,
                            tool_calls[index + 1 :],
                        )
                        agent_span.set_attributes(
                            final_answer_chars=len(result.content or ""),
                            steps=step,
                            stopped_reason="waiting_approval",
                        )
                        trace.add(
                            "waiting_approval",
                            approval_id=result.approval_id,
                            session_id=context.session_id,
                        )
                        return result.content, trace

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

    def approve_tool(self, approval_id: str, user_id: str, scope: str = "once") -> str:
        gate = getattr(self.tools, "approval_gate", None)
        if gate is None:
            return "审批通道不可用"
        _ok, message = gate.approve(approval_id, user_id, scope)
        return message

    def deny_tool(self, approval_id: str, user_id: str) -> str:
        gate = getattr(self.tools, "approval_gate", None)
        if gate is None:
            return "审批通道不可用"
        _ok, message = gate.deny(approval_id, user_id)
        return message

    def resume_after_approval(self, approval_id: str) -> tuple[str, Trace]:
        gate = getattr(self.tools, "approval_gate", None)
        pending = gate.get(approval_id) if gate is not None else None
        if pending is None or pending.kind != "scheduled":
            return "未找到待审批的调度操作", Trace()

        target = pending.target or {}
        context = RunContext.create(
            session_id=pending.request.session_id or "default",
            workspace=self.settings.workspace,
            entrypoint="scheduler_resume",
            channel_type=target.get("channel_type") or pending.request.channel_type,
            user_id=target.get("user_id") or pending.request.user_id,
            is_group=bool(target.get("is_group")),
            group_id=target.get("group_id"),
            is_scheduled_task=True,
            task_id=pending.request.task_id,
            resume_approval_id=approval_id,
            resume_kind=pending.resume_kind,
        )
        trace = context.trace
        with trace.start_span(
            "agent.resume_after_approval",
            span_kind="agent.invoke",
            session_id=context.session_id,
            approval_id=approval_id,
            outcome=pending.status,
            resume_kind=pending.resume_kind,
        ) as agent_span:
            if pending.resume_kind == "tool_call":
                answer = self._resume_direct_tool(pending, context)
                agent_span.set_attributes(
                    final_answer_chars=len(answer or ""),
                    stopped_reason="tool_call_resume",
                )
                return answer, trace
            return self._resume_agent_loop(pending, context, agent_span)

    def _park_scheduled_call(self, context: RunContext, result: Any, call: dict, remaining: list) -> None:
        gate = getattr(self.tools, "approval_gate", None)
        if gate is not None and result.approval_id:
            gate.attach_resume_state(
                result.approval_id,
                tool_call_id=str(call.get("id") or ""),
                remaining_tool_calls=list(remaining or []),
                resume_kind="agent_loop",
            )
        context.add_metadata(
            waiting_approval={
                "approval_id": result.approval_id,
                "session_id": context.session_id,
                "prompt": result.content,
                "tool_call_id": str(call.get("id") or ""),
            }
        )

    def _resume_direct_tool(self, pending: Any, context: RunContext) -> str:
        if pending.status == "approved":
            result = self.tools.run(pending.request.name, pending.request.arguments, context)
            context.add_metadata(resume_approval_id="")
            return result.content or ""
        result = self._approval_failure_result(pending)
        return result.content

    def _resume_agent_loop(self, pending: Any, context: RunContext, agent_span: Any) -> tuple[str, Trace]:
        context_manager = self._get_context_manager(pending.request.session_id)
        if pending.status == "approved":
            result = self.tools.run(pending.request.name, pending.request.arguments, context)
        else:
            result = self._approval_failure_result(pending)
        context.add_metadata(resume_approval_id="")
        context_manager.append_tool_result(
            tool_call_id=pending.tool_call_id,
            name=pending.request.name,
            content=result.content,
        )
        self.memory.record_message(
            session_id=context.session_id,
            role="tool",
            content=result.content,
            name=pending.request.name,
            metadata={"tool_call_id": pending.tool_call_id, "ok": result.ok},
        )
        for call in list(pending.remaining_tool_calls or []):
            function = call.get("function") or {}
            tool_name = function.get("name", "")
            arguments = function.get("arguments", "{}")
            next_result = self.tools.run(tool_name, arguments, context)
            if getattr(next_result, "waiting_approval", False):
                self._park_scheduled_call(context, next_result, call, [])
                context.trace.add("waiting_approval", approval_id=next_result.approval_id)
                agent_span.set_attributes(stopped_reason="waiting_approval")
                return next_result.content, context.trace
            context_manager.append_tool_result(
                tool_call_id=call.get("id"),
                name=tool_name,
                content=next_result.content,
            )
            self.memory.record_message(
                session_id=context.session_id,
                role="tool",
                content=next_result.content,
                name=tool_name,
                metadata={"tool_call_id": call.get("id"), "ok": next_result.ok},
            )
        return self._continue_steps(context, context_manager, agent_span)

    def _continue_steps(self, context: RunContext, context_manager: Any, agent_span: Any) -> tuple[str, Trace]:
        trace = context.trace
        for step in range(1, self.settings.max_steps + 1):
            tool_schemas = self.tools.schemas(context)
            model_messages = context_manager.build_model_messages(
                context,
                tool_schemas=tool_schemas,
            )
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
            for index, call in enumerate(tool_calls):
                function = call.get("function") or {}
                tool_name = function.get("name", "")
                arguments = function.get("arguments", "{}")
                result = self.tools.run(tool_name, arguments, context)
                if getattr(result, "waiting_approval", False):
                    self._park_scheduled_call(context, result, call, tool_calls[index + 1 :])
                    agent_span.set_attributes(stopped_reason="waiting_approval")
                    trace.add("waiting_approval", approval_id=result.approval_id)
                    return result.content, trace
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
                    metadata={"tool_call_id": call.get("id"), "ok": result.ok},
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
    def _approval_failure_result(pending: Any):
        from tools.base import ToolResult

        reason = "timed out" if pending.status == "timeout" else "denied"
        return ToolResult(
            ok=False,
            content=(
                f"Tool approval {reason}: {pending.request.name} "
                f"(risk={pending.request.risk_level}, id={pending.approval_id})"
            ),
        )

    def summarize_session(self, session_id: str) -> str:
        context_manager = self._get_context_manager(session_id)
        context = RunContext.create(
            session_id=session_id,
            workspace=self.settings.workspace,
            entrypoint="summarize",
        )
        compacted = context_manager.compact_now(
            context,
            tool_schemas=self.tools.schemas(context),
        )
        if compacted:
            return "已压缩"
        return "无需压缩"

    def _load_compaction_prompt(self) -> str:
        path = self.settings.project_root / "prompts" / "compaction.md"
        if path.exists():
            content = path.read_text(encoding="utf-8").strip()
            if content:
                return content
        return (
            "Compress older conversation turns into a short working summary. "
            "Include user goals, files changed, remaining tasks, compact_count, "
            "and the original transcript path. Return only the summary body."
        )

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
