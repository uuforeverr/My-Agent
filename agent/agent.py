import hashlib
import json
import threading
import time
from typing import Any, Callable, Optional

from agent.compactor import Compactor
from agent.context_manager import ContextManager
from agent.run_context import RunContext, TurnResult
from agent.token_counter import TokenCounter
from config import Settings
from llm.openai_client import LLMError, OpenAICompatibleClient
from memory import MemoryManager
from observability.otel import add_content_event, add_event, set_attributes, set_error, start_span, trace_id_hex
from observability.correlation import TASK_ID_ATTR, TASK_IDS_ATTR
from prompts import PromptLoader
from scheduler.repository import SchedulerRepository
from state import StateDatabase
from skills import SkillLoader
from tools.approval import ApprovalGate
from tools.registry import ToolRegistry


class Agent:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.llm = OpenAICompatibleClient(settings)
        self.prompt_loader = PromptLoader(settings.project_root / "prompts" / "identity.md")
        state_db_path = settings.state_db_path
        if not state_db_path.is_absolute():
            state_db_path = settings.project_root / state_db_path
        self.state_database = StateDatabase(state_db_path)
        self.memory = MemoryManager(
            settings.project_root / "data" / "memory",
            llm=self.llm,
            state_database=self.state_database,
            embedding_provider=settings.memory_embedding_provider,
            embedding_model=settings.memory_embedding_model,
            embedding_device=settings.memory_embedding_device,
            embedding_local_files_only=settings.memory_embedding_local_files_only,
            vector_backend=settings.memory_vector_backend,
            candidate_limit=settings.memory_candidate_limit,
            rrf_k=settings.memory_rrf_k,
            semantic_max_chars=settings.memory_semantic_max_chars,
            raw_write_batch_size=settings.raw_write_batch_size,
            raw_write_flush_interval_ms=settings.raw_write_flush_interval_ms,
            raw_write_queue_capacity=settings.raw_write_queue_capacity,
            episodic_batch_turns=settings.episodic_batch_turns,
            episodic_max_wait_seconds=settings.episodic_max_wait_seconds,
            episodic_worker_poll_seconds=settings.episodic_worker_poll_seconds,
            episodic_body_max_chars=settings.episodic_body_max_chars,
        )
        self.skill_loader = SkillLoader(settings.project_root / "skills")
        self.scheduler_store = None
        if settings.scheduler_enabled:
            self.scheduler_store = SchedulerRepository(self.state_database)
            self.scheduler_store.migrate_legacy_json(settings.scheduler_tasks_path)
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
            sandbox_enabled=settings.sandbox_enabled,
            sandbox_image=settings.sandbox_image,
            sandbox_network=settings.sandbox_network,
            sandbox_memory=settings.sandbox_memory,
            sandbox_cpus=settings.sandbox_cpus,
            sandbox_pids_limit=settings.sandbox_pids_limit,
            sandbox_user=settings.sandbox_user,
        )
        self.identity_prompt = self.prompt_loader.load_identity()
        self.compaction_prompt = self._load_compaction_prompt()
        self._context_managers: dict[str, ContextManager] = {}
        self._context_managers_lock = threading.Lock()

    @staticmethod
    def _tool_schema_names(tool_schemas: list[dict[str, Any]]) -> list[str]:
        """Return the exact function names exposed on a model request."""
        names: list[str] = []
        for schema in tool_schemas:
            function = schema.get("function") or {}
            name = function.get("name") or schema.get("name")
            if name:
                names.append(str(name))
        return names

    def _get_context_manager(self, session_id: str) -> ContextManager:
        with self._context_managers_lock:
            manager = self._context_managers.get(session_id)
            if manager is None:
                manager = ContextManager(
                    self.identity_prompt,
                    memory=self.memory,
                    skill_loader=self.skill_loader,
                    memory_search_results=self.settings.memory_search_results,
                    session_window_turns=self.settings.session_window_turns,
                    token_counter=TokenCounter(
                        model=self.settings.model,
                        window_override=self.settings.model_context_window,
                    ),
                    compactor=Compactor(lambda: self.llm, self.compaction_prompt),
                )
                self._context_managers[session_id] = manager
                if not manager.history:
                    manager.restore_compaction_state(
                        self.memory.load_compaction_state(session_id)
                    )
                    self.memory.recover_pending(
                        session_id,
                        self.settings.session_window_turns,
                        summarized_through_turn_seq=manager.summarized_through_turn_seq,
                        flush_raw=False,
                    )
                    manager.history = self._hydrate_history(
                        session_id,
                        after_turn_seq=manager.summarized_through_turn_seq,
                    )
            return manager

    def _hydrate_history(
        self,
        session_id: str,
        *,
        after_turn_seq: int = 0,
    ) -> list[dict[str, Any]]:
        turns = self.memory.raw_store.read_recent_turns(
            session_id,
            self.settings.session_window_turns,
            flush=False,
        )
        self.memory.raw_store.seed_session_sequence(
            session_id,
            max((int(turn.get("turn_seq") or 0) for turn in turns), default=0),
        )
        turns = [
            turn
            for turn in turns
            if int(turn.get("turn_seq") or 0) > int(after_turn_seq)
        ]
        records = [record for turn in turns for record in turn["messages"]]
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
            "_turn_seq": int(record.get("turn_seq") or 0),
            "_created_at": record.get("created_at") or "",
        }
        metadata = record.get("metadata") or {}
        if role == "assistant":
            tool_calls = metadata.get("tool_calls")
            if tool_calls:
                message["tool_calls"] = tool_calls
            response_items = metadata.get("response_items")
            if response_items:
                message["response_items"] = response_items
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
    ) -> tuple[str, TurnResult]:
        context = context or RunContext.create(
            session_id="default",
            workspace=self.settings.workspace,
        )
        if on_event is not None:
            context.add_metadata(on_event=on_event)
        invoke_started = time.monotonic()
        correlated_task_ids: list[str] = []
        correction_counts: dict[tuple[str, str], int] = {}
        recovered_error_count = 0
        with start_span(
            "invoke_agent my-agent",
            attributes={
                "gen_ai.operation.name": "invoke_agent",
                "gen_ai.agent.name": "my-agent",
                "my_agent.session.id": context.session_id,
                "my_agent.entrypoint": context.get("entrypoint"),
                "messaging.system": context.get("channel_type"),
                "my_agent.user_message.chars": len(user_message),
            },
        ) as agent_span:
            add_content_event(agent_span, "my_agent.user_message", user_message)
            context_manager = self._get_context_manager(context.session_id)
            receipt = self.memory.record_message(
                session_id=context.session_id,
                role="user",
                content=user_message,
                metadata={
                    "entrypoint": context.get("entrypoint"),
                    "user_id": str(context.get("user_id") or "local"),
                    "channel_type": str(context.get("channel_type") or ""),
                },
            )
            context_manager.append_user_message(user_message, turn_seq=receipt.turn_seq)
            add_event(
                agent_span,
                "user_message",
                {"chars": len(user_message), "my_agent.session.id": context.session_id},
            )

            for step in range(1, self.settings.max_steps + 1):
                with start_span(
                    f"agent.step {step}",
                    attributes={
                        "my_agent.step": step,
                        "my_agent.session.id": context.session_id,
                    },
                ):
                    tool_schemas = self.tools.schemas(context)
                    model_messages = context_manager.build_model_messages(
                        context,
                        tool_schemas=tool_schemas,
                    )
                    llm_start = time.monotonic()
                    with start_span(
                        f"chat {self.settings.model}",
                        attributes={
                            "gen_ai.operation.name": "chat",
                            "gen_ai.request.model": self.settings.model,
                            "my_agent.step": step,
                            "my_agent.session.id": context.session_id,
                            "my_agent.message_count": len(model_messages),
                            "my_agent.tool_schema_count": len(tool_schemas),
                            "my_agent.tool.names": self._tool_schema_names(tool_schemas),
                        },
                    ) as llm_span:
                        add_content_event(llm_span, "gen_ai.client.inference.operation.details", model_messages)
                        message = self._call_model(model_messages, tool_schemas, on_event)
                        tool_calls = message.get("tool_calls") or []
                        response_metadata = getattr(self.llm, "last_response_metadata", {}) or {}
                        usage = response_metadata.get("usage") or {}
                        set_attributes(
                            llm_span,
                            **{
                                "gen_ai.response.model": response_metadata.get("model") or self.settings.model,
                                "gen_ai.response.id": response_metadata.get("response_id"),
                                "gen_ai.usage.input_tokens": usage.get("prompt_tokens"),
                                "gen_ai.usage.output_tokens": usage.get("completion_tokens"),
                                "my_agent.usage.reasoning_tokens": usage.get("reasoning_tokens"),
                                "my_agent.llm.response_content_chars": len(message.get("content") or ""),
                                "my_agent.llm.tool_call_count": len(tool_calls),
                                "my_agent.llm.finish_reason": response_metadata.get("finish_reason") or ("tool_calls" if tool_calls else "stop"),
                                "my_agent.llm.time_to_first_event_ms": response_metadata.get("time_to_first_event_ms"),
                                "my_agent.llm.time_to_first_output_text_ms": response_metadata.get("time_to_first_output_text_ms"),
                                "my_agent.llm.stream_duration_ms": response_metadata.get("stream_duration_ms"),
                            },
                        )
                        add_content_event(llm_span, "gen_ai.client.inference.result", message)

                    add_event(
                        agent_span,
                        "llm_call",
                        {
                            "step": step,
                            "my_agent.session.id": context.session_id,
                            "duration_ms": round((time.monotonic() - llm_start) * 1000, 2),
                        },
                    )

                    context_manager.append_assistant_message(message)
                    self.memory.record_message(
                        session_id=context.session_id,
                        role="assistant",
                        content=message.get("content") or "",
                        metadata={
                            "tool_calls": message.get("tool_calls") or [],
                            "response_items": message.get("response_items") or [],
                        },
                    )

                    if not tool_calls:
                        answer = message.get("content") or ""
                        set_attributes(
                            agent_span,
                            **{
                                "my_agent.final_answer.chars": len(answer),
                                "my_agent.steps": step,
                                "my_agent.stopped_reason": "final_answer",
                                "my_agent.run.recovered_error_count": recovered_error_count,
                            },
                        )
                        add_content_event(agent_span, "my_agent.final_answer", answer)
                        add_event(agent_span, "final_answer", {"total_ms": round((time.monotonic() - invoke_started) * 1000, 2)})
                        return answer, TurnResult(
                            outcome="success",
                            trace_id=trace_id_hex(agent_span),
                            recovered_error_count=recovered_error_count,
                        )

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
                        if not result.ok and not result.waiting_approval:
                            recovered_error_count += 1
                        result = self._apply_correction_limit(
                            result,
                            tool_name,
                            arguments,
                            correction_counts,
                        )
                        correlated_task_id = str(result.metadata.get(TASK_ID_ATTR) or "")
                        if correlated_task_id and correlated_task_id not in correlated_task_ids:
                            correlated_task_ids.append(correlated_task_id)
                            set_attributes(
                                agent_span,
                                **{
                                    TASK_ID_ATTR: correlated_task_ids[0],
                                    TASK_IDS_ATTR: list(correlated_task_ids),
                                }
                            )
                        parked = bool(getattr(result, "waiting_approval", False))
                        add_event(
                            agent_span,
                            "tool_call",
                            {
                                "step": step,
                                "my_agent.session.id": context.session_id,
                                "gen_ai.tool.name": tool_name,
                                "status": "waiting_approval" if parked else ("success" if result.ok else "error"),
                                "duration_ms": round((time.monotonic() - tool_start) * 1000, 2),
                            },
                        )
                        if parked:
                            self._park_scheduled_call(
                                context,
                                result,
                                call,
                                tool_calls[index + 1 :],
                            )
                            set_attributes(
                                agent_span,
                                **{
                                    "my_agent.final_answer.chars": len(result.content or ""),
                                    "my_agent.steps": step,
                                    "my_agent.stopped_reason": "waiting_approval",
                                },
                            )
                            add_event(
                                agent_span,
                                "waiting_approval",
                                {
                                    "my_agent.approval.id": result.approval_id,
                                    "my_agent.session.id": context.session_id,
                                },
                            )
                            return result.content, TurnResult(
                                outcome="waiting_approval",
                                trace_id=trace_id_hex(agent_span),
                                recovered_error_count=recovered_error_count,
                                waiting_approval_id=result.approval_id,
                            )

                        model_output = result.function_output(tool_name, str(call.get("id") or ""))
                        context_manager.append_tool_result(
                            tool_call_id=call.get("id"),
                            name=tool_name,
                            content=model_output,
                        )
                        self.memory.record_message(
                            session_id=context.session_id,
                            role="tool",
                            content=model_output,
                            name=tool_name,
                            metadata={
                                "tool_call_id": call.get("id"),
                                "ok": result.ok,
                                "error_code": result.error_code,
                                "retryable": result.retryable,
                            },
                        )

            fallback = f"Reached max_steps={self.settings.max_steps}; stopping to avoid an infinite tool loop."
            set_attributes(
                agent_span,
                **{
                    "my_agent.final_answer.chars": len(fallback),
                    "my_agent.steps": self.settings.max_steps,
                    "my_agent.stopped_reason": "max_steps_reached",
                },
            )
            set_error(agent_span, "max_steps reached")
            add_event(agent_span, "max_steps_reached", {"total_ms": round((time.monotonic() - invoke_started) * 1000, 2)})
            context_manager.append_assistant_message({"role": "assistant", "content": fallback})
            self.memory.record_message(
                session_id=context.session_id,
                role="assistant",
                content=fallback,
                metadata={"reason": "max_steps_reached"},
            )
            return fallback, TurnResult(
                outcome="max_steps",
                trace_id=trace_id_hex(agent_span),
                recovered_error_count=recovered_error_count,
            )

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

    def resume_after_approval(self, approval_id: str) -> tuple[str, TurnResult]:
        gate = getattr(self.tools, "approval_gate", None)
        pending = gate.get(approval_id) if gate is not None else None
        if pending is None or pending.kind != "scheduled":
            return "未找到待审批的调度操作", TurnResult(outcome="not_found")

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
        with start_span(
            "invoke_agent my-agent",
            attributes={
                "gen_ai.operation.name": "invoke_agent",
                "my_agent.session.id": context.session_id,
                "my_agent.approval.id": approval_id,
                "my_agent.approval.outcome": pending.status,
                "my_agent.resume.kind": pending.resume_kind,
            },
        ) as agent_span:
            add_content_event(
                agent_span,
                "my_agent.resume.input",
                {"approval_id": approval_id, "outcome": pending.status, "tool_name": pending.request.name},
            )
            if pending.resume_kind == "tool_call":
                answer = self._resume_direct_tool(pending, context)
                set_attributes(
                    agent_span,
                    **{
                        "my_agent.final_answer.chars": len(answer or ""),
                        "my_agent.stopped_reason": "tool_call_resume",
                    },
                )
                add_content_event(agent_span, "my_agent.final_answer", answer or "")
                return answer, TurnResult(outcome="success", trace_id=trace_id_hex(agent_span))
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

    def _resume_agent_loop(self, pending: Any, context: RunContext, agent_span: Any) -> tuple[str, TurnResult]:
        context_manager = self._get_context_manager(pending.request.session_id)
        if pending.status == "approved":
            result = self.tools.run(pending.request.name, pending.request.arguments, context)
        else:
            result = self._approval_failure_result(pending)
        context.add_metadata(resume_approval_id="")
        model_output = result.function_output(pending.request.name, pending.tool_call_id)
        context_manager.append_tool_result(
            tool_call_id=pending.tool_call_id,
            name=pending.request.name,
            content=model_output,
        )
        self.memory.record_message(
            session_id=context.session_id,
            role="tool",
            content=model_output,
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
                add_event(agent_span, "waiting_approval", {"my_agent.approval.id": next_result.approval_id})
                set_attributes(
                    agent_span,
                    **{"my_agent.stopped_reason": "waiting_approval"},
                )
                return next_result.content, TurnResult(
                    outcome="waiting_approval",
                    trace_id=trace_id_hex(agent_span),
                    waiting_approval_id=next_result.approval_id,
                )
            next_output = next_result.function_output(tool_name, str(call.get("id") or ""))
            context_manager.append_tool_result(
                tool_call_id=call.get("id"),
                name=tool_name,
                content=next_output,
            )
            self.memory.record_message(
                session_id=context.session_id,
                role="tool",
                content=next_output,
                name=tool_name,
                metadata={"tool_call_id": call.get("id"), "ok": next_result.ok},
            )
        return self._continue_steps(context, context_manager, agent_span)

    def _continue_steps(self, context: RunContext, context_manager: Any, agent_span: Any) -> tuple[str, TurnResult]:
        started = time.monotonic()
        for step in range(1, self.settings.max_steps + 1):
            with start_span(
                f"agent.step {step}",
                attributes={
                    "my_agent.step": step,
                    "my_agent.session.id": context.session_id,
                },
            ):
                tool_schemas = self.tools.schemas(context)
                model_messages = context_manager.build_model_messages(
                    context,
                    tool_schemas=tool_schemas,
                )
                llm_start = time.monotonic()
                with start_span(
                    f"chat {self.settings.model}",
                    attributes={
                        "gen_ai.operation.name": "chat",
                        "gen_ai.request.model": self.settings.model,
                        "my_agent.step": step,
                        "my_agent.session.id": context.session_id,
                        "my_agent.message_count": len(model_messages),
                        "my_agent.tool_schema_count": len(tool_schemas),
                        "my_agent.tool.names": self._tool_schema_names(tool_schemas),
                    },
                ) as llm_span:
                    add_content_event(llm_span, "gen_ai.client.inference.operation.details", model_messages)
                    message = self._call_model(
                        model_messages,
                        tool_schemas,
                        context.get("on_event"),
                    )
                    tool_calls = message.get("tool_calls") or []
                    response_metadata = getattr(self.llm, "last_response_metadata", {}) or {}
                    usage = response_metadata.get("usage") or {}
                    set_attributes(
                        llm_span,
                        **{
                            "gen_ai.response.model": response_metadata.get("model") or self.settings.model,
                            "gen_ai.response.id": response_metadata.get("response_id"),
                            "gen_ai.usage.input_tokens": usage.get("prompt_tokens"),
                            "gen_ai.usage.output_tokens": usage.get("completion_tokens"),
                            "my_agent.llm.response_content_chars": len(message.get("content") or ""),
                            "my_agent.llm.tool_call_count": len(tool_calls),
                        },
                    )
                    add_content_event(llm_span, "gen_ai.client.inference.result", message)
                add_event(
                    agent_span,
                    "llm_call",
                    {
                        "step": step,
                        "my_agent.session.id": context.session_id,
                        "duration_ms": round((time.monotonic() - llm_start) * 1000, 2),
                    },
                )
                context_manager.append_assistant_message(message)
                self.memory.record_message(
                    session_id=context.session_id,
                    role="assistant",
                    content=message.get("content") or "",
                    metadata={
                        "tool_calls": message.get("tool_calls") or [],
                        "response_items": message.get("response_items") or [],
                    },
                )
                if not tool_calls:
                    answer = message.get("content") or ""
                    set_attributes(
                        agent_span,
                        **{
                            "my_agent.final_answer.chars": len(answer),
                            "my_agent.steps": step,
                            "my_agent.stopped_reason": "final_answer",
                        },
                    )
                    add_content_event(agent_span, "my_agent.final_answer", answer)
                    add_event(agent_span, "final_answer", {"total_ms": round((time.monotonic() - started) * 1000, 2)})
                    return answer, TurnResult(outcome="success", trace_id=trace_id_hex(agent_span))
                for index, call in enumerate(tool_calls):
                    function = call.get("function") or {}
                    tool_name = function.get("name", "")
                    arguments = function.get("arguments", "{}")
                    result = self.tools.run(tool_name, arguments, context)
                    if getattr(result, "waiting_approval", False):
                        self._park_scheduled_call(context, result, call, tool_calls[index + 1 :])
                        set_attributes(agent_span, **{"my_agent.stopped_reason": "waiting_approval"})
                        add_event(agent_span, "waiting_approval", {"my_agent.approval.id": result.approval_id})
                        return result.content, TurnResult(
                            outcome="waiting_approval",
                            trace_id=trace_id_hex(agent_span),
                            waiting_approval_id=result.approval_id,
                        )
                    model_output = result.function_output(tool_name, str(call.get("id") or ""))
                    context_manager.append_tool_result(
                        tool_call_id=call.get("id"),
                        name=tool_name,
                        content=model_output,
                    )
                    self.memory.record_message(
                        session_id=context.session_id,
                        role="tool",
                        content=model_output,
                        name=tool_name,
                        metadata={"tool_call_id": call.get("id"), "ok": result.ok},
                    )
        fallback = f"Reached max_steps={self.settings.max_steps}; stopping to avoid an infinite tool loop."
        set_attributes(
            agent_span,
            **{
                "my_agent.final_answer.chars": len(fallback),
                "my_agent.steps": self.settings.max_steps,
                "my_agent.stopped_reason": "max_steps_reached",
            },
        )
        set_error(agent_span, "max_steps reached")
        add_event(agent_span, "max_steps_reached", {"total_ms": round((time.monotonic() - started) * 1000, 2)})
        context_manager.append_assistant_message({"role": "assistant", "content": fallback})
        self.memory.record_message(
            session_id=context.session_id,
            role="assistant",
            content=fallback,
            metadata={"reason": "max_steps_reached"},
        )
        return fallback, TurnResult(outcome="max_steps", trace_id=trace_id_hex(agent_span))

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
            error_code="approval_timeout" if pending.status == "timeout" else "approval_denied",
            error_type="permission_error",
            retryable=False,
            suggested_action="Explain that the tool was not executed",
        )

    def _call_model(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        on_event: Optional[Callable[[dict[str, Any]], None]],
    ) -> dict[str, Any]:
        def _stream_event(event: dict[str, Any]) -> None:
            event_type = event.get("type")
            delta = str(event.get("delta") or "")
            if event_type == "output_text_delta" and delta:
                self._emit_event(on_event, {"type": "assistant_delta", "content": delta})
            elif event_type == "reasoning_summary_delta" and delta:
                self._emit_event(on_event, {"type": "assistant_progress_delta", "content": delta})

        responder = getattr(self.llm, "respond", None)
        if not callable(responder):
            return self.llm.chat(messages, tools=tools)

        last_error: Optional[Exception] = None
        for attempt in range(1, 4):
            try:
                result = responder(messages, tools=tools, on_event=_stream_event)
                return result.message
            except LLMError as error:
                last_error = error
                if attempt >= 3:
                    break
                self._emit_event(
                    on_event,
                    {
                        "type": "assistant_progress",
                        "content": "模型流中断，正在重新请求…",
                        "attempt": attempt + 1,
                    },
                )
        raise LLMError(f"Responses stream retry limit exceeded: {last_error}") from last_error

    @staticmethod
    def _apply_correction_limit(
        result: Any,
        tool_name: str,
        arguments: Any,
        counts: dict[tuple[str, str], int],
    ) -> Any:
        if result.ok or not result.retryable:
            return result
        try:
            parsed = json.loads(arguments) if isinstance(arguments, str) else arguments
            normalized = json.dumps(parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except Exception:
            normalized = str(arguments)
        fingerprint = hashlib.sha256(f"{tool_name}:{normalized}".encode("utf-8")).hexdigest()
        key = (fingerprint, result.error_code or "execution_error")
        counts[key] = counts.get(key, 0) + 1
        if counts[key] <= 2:
            return result
        result.error_code = "retry_limit_exceeded"
        result.error_type = "retry_limit"
        result.retryable = False
        result.suggested_action = "Explain the repeated failure to the user"
        return result

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
            "Merge the previous working summary and all completed turns before "
            "the current turn into one concise working summary. Preserve goals, "
            "requirements, decisions, completed work, files, commands, progress, "
            "blockers, failed approaches, remaining tasks, compact_count, and the "
            "transcript path. Return only the summary body."
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
