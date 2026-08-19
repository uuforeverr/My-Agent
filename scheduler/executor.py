from typing import Any, Callable, Optional

from channel.reply import Reply
from channel.target import ChannelTarget
from observability import JsonlTraceStore, Trace

WAITING_APPROVAL = "waiting_approval"

# Injected collaborators (wired up by ChannelManager):
#   send_reply(target, reply) -> bool         deliver text to a ChannelTarget
#   run_agent(message, context) -> (answer, trace)   the Agent.run signature
#   run_tool(name, arguments, context) -> ToolResult  the ToolRegistry.run signature
#   run_dream(lookback_days) -> dict          run memory deep-dream consolidation
#   run_scan(keep_recent_turns, min_flush_turns) -> dict
#   build_context(session_id, **metadata) -> RunContext
SendReply = Callable[[ChannelTarget, Reply], bool]
RunAgent = Callable[..., Any]
RunTool = Callable[..., Any]
RunDream = Callable[..., Any]
RunScan = Callable[..., Any]
BuildContext = Callable[..., Any]


class SchedulerExecutor:
    """Runs a due task's action and delivers the outcome to its channel target.

    Returns True on successful delivery. Returns False when delivery could not
    happen (e.g. channel not ready) so the service keeps next_run_at and retries
    on the next tick. Raising is also treated as failure by the service.
    """

    def __init__(
        self,
        send_reply: SendReply,
        run_agent: Optional[RunAgent] = None,
        run_tool: Optional[RunTool] = None,
        run_dream: Optional[RunDream] = None,
        run_scan: Optional[RunScan] = None,
        build_context: Optional[BuildContext] = None,
        trace_store: Optional[JsonlTraceStore] = None,
    ):
        self.send_reply = send_reply
        self.run_agent = run_agent
        self.run_tool = run_tool
        self.run_dream = run_dream
        self.run_scan = run_scan
        self.build_context = build_context
        self.trace_store = trace_store

    def execute(self, task: dict[str, Any]):
        action = task.get("action") or {}
        action_type = action.get("type")
        trace = Trace()
        trace.add(
            "scheduler_execute_start",
            task_id=task.get("id"),
            action_type=action_type,
        )

        try:
            with trace.start_span(
                "scheduler.execute",
                span_kind="scheduler.execute",
                task_id=task.get("id"),
                action_type=action_type,
            ) as span:
                if action_type == "memory_dream":
                    result = self._memory_dream(task, action, trace)
                elif action_type == "memory_scan_episodic":
                    result = self._memory_scan_episodic(task, action, trace)
                else:
                    target = ChannelTarget.from_dict(action.get("target") or {})
                    if not target.channel_type:
                        raise ValueError(f"task {task.get('id')} has no delivery target")

                    if action_type == "send_message":
                        result = self._send_message(action, target)
                    elif action_type == "agent_task":
                        result = self._agent_task(task, action, target, trace)
                    elif action_type == "tool_call":
                        result = self._tool_call(task, action, target, trace)
                    else:
                        raise ValueError(f"unknown action type: {action_type!r}")

                parked = result == WAITING_APPROVAL
                delivered = result is not False
                span.set_attribute("delivered", delivered)
                span.set_attribute("parked", parked)
                if result is False:
                    span.set_status("retry")
                trace.add(
                    "scheduler_execute_completed",
                    task_id=task.get("id"),
                    action_type=action_type,
                    delivered=delivered,
                    parked=parked,
                )
                trace.finish("success" if delivered else "retry")
                return result
        except Exception as e:
            trace.record_error(
                e,
                event_name="scheduler_execute_error",
                task_id=task.get("id"),
                action_type=action_type,
            )
            raise
        finally:
            self._persist_trace(trace)

    def _memory_dream(self, task: dict[str, Any], action: dict[str, Any], trace: Trace) -> bool:
        if self.run_dream is None:
            raise ValueError("memory_dream requires run_dream")

        try:
            lookback_days = int(action.get("lookback_days") or 1)
        except (TypeError, ValueError) as e:
            raise ValueError("memory_dream lookback_days must be an integer") from e
        if lookback_days <= 0:
            raise ValueError("memory_dream lookback_days must be > 0")

        result = self.run_dream(lookback_days=lookback_days)
        trace.add(
            "scheduler_memory_dream_completed",
            task_id=task.get("id"),
            lookback_days=lookback_days,
            ok=bool((result or {}).get("ok")) if isinstance(result, dict) else True,
        )
        return True

    def _memory_scan_episodic(self, task: dict[str, Any], action: dict[str, Any], trace: Trace) -> bool:
        if self.run_scan is None:
            raise ValueError("memory_scan_episodic requires run_scan")

        try:
            keep_recent_turns = int(action.get("keep_recent_turns") or 3)
            min_flush_turns = int(action.get("min_flush_turns") or 20)
        except (TypeError, ValueError) as e:
            raise ValueError("memory_scan_episodic keep_recent_turns/min_flush_turns must be integers") from e
        if keep_recent_turns <= 0:
            raise ValueError("memory_scan_episodic keep_recent_turns must be > 0")
        if min_flush_turns < 0:
            raise ValueError("memory_scan_episodic min_flush_turns must be >= 0")

        result = self.run_scan(
            keep_recent_turns=keep_recent_turns,
            min_flush_turns=min_flush_turns,
        )
        trace.add(
            "scheduler_memory_scan_completed",
            task_id=task.get("id"),
            keep_recent_turns=keep_recent_turns,
            min_flush_turns=min_flush_turns,
            ok=bool((result or {}).get("ok")) if isinstance(result, dict) else True,
            skipped=bool((result or {}).get("skipped")) if isinstance(result, dict) else False,
        )
        return True

    def _send_message(self, action: dict[str, Any], target: ChannelTarget) -> bool:
        content = (action.get("content") or "").strip()
        if not content:
            raise ValueError("send_message action has empty content")
        return self._deliver(target, content)

    def _agent_task(
        self,
        task: dict[str, Any],
        action: dict[str, Any],
        target: ChannelTarget,
        trace: Trace,
    ) -> bool:
        if self.run_agent is None or self.build_context is None:
            raise ValueError("agent_task requires run_agent and build_context")
        ai_task = (action.get("ai_task") or "").strip()
        if not ai_task:
            raise ValueError("agent_task action has empty ai_task")

        context = self._scheduler_context(task, target, trace=trace)
        answer, _trace = self.run_agent(ai_task, context=context)
        waiting = context.get("waiting_approval") if context is not None else None
        if waiting:
            task["pending_approval_id"] = waiting.get("approval_id") or ""
            task["parked_session_id"] = context.session_id
            prompt = (waiting.get("prompt") or answer or "").strip()
            if prompt:
                self._deliver(target, prompt)
            return WAITING_APPROVAL
        answer = (answer or "").strip()
        trace.add(
            "scheduler_agent_task_completed",
            task_id=task.get("id"),
            answer_chars=len(answer),
        )
        if not answer:
            return True
        return self._deliver(target, answer)

    def _tool_call(
        self,
        task: dict[str, Any],
        action: dict[str, Any],
        target: ChannelTarget,
        trace: Trace,
    ) -> bool:
        if self.run_tool is None:
            raise ValueError("tool_call requires run_tool")
        tool_name = (action.get("tool_name") or "").strip()
        if not tool_name:
            raise ValueError("tool_call action has empty tool_name")

        context = self._scheduler_context(task, target, trace=trace, resume_kind="tool_call")
        result = self.run_tool(tool_name, action.get("tool_arguments") or {}, context)
        if getattr(result, "waiting_approval", False):
            task["pending_approval_id"] = getattr(result, "approval_id", "") or ""
            task["parked_session_id"] = context.session_id
            content = getattr(result, "content", "") or ""
            if content.strip():
                self._deliver(target, content)
            return WAITING_APPROVAL
        content = getattr(result, "content", str(result))
        trace.add(
            "scheduler_tool_call_completed",
            task_id=task.get("id"),
            tool_name=tool_name,
            ok=bool(getattr(result, "ok", True)),
            result_chars=len(content or ""),
        )
        if not (content or "").strip():
            return True
        return self._deliver(target, content)

    def _scheduler_context(self, task: dict[str, Any], target: ChannelTarget, trace: Trace, **extra):
        session_id = f"scheduler:{target.session_id or target.user_id}:{task.get('id')}"
        return self.build_context(
            session_id=session_id,
            trace=trace,
            entrypoint="scheduler",
            channel_type=target.channel_type,
            user_id=target.user_id,
            is_group=target.is_group,
            group_id=target.group_id,
            is_scheduled_task=True,
            task_id=str(task.get("id") or ""),
            **extra,
        )

    def _deliver(self, target: ChannelTarget, content: str) -> bool:
        return bool(self.send_reply(target, Reply(content)))

    def _persist_trace(self, trace: Trace) -> None:
        if self.trace_store is None:
            return
        try:
            self.trace_store.append(trace)
        except Exception:
            pass
