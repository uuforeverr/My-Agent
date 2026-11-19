from dataclasses import dataclass
from typing import Any, Callable, Optional

from channel.reply import Reply
from channel.target import ChannelTarget

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


@dataclass
class ExecutionResult:
    status: str
    content: str = ""
    trace_id: str = ""
    error: str = ""
    pending_approval_id: str = ""
    parked_session_id: str = ""


    @property
    def succeeded(self) -> bool:
        return self.status == "succeeded"


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
        trace_store: Optional[Any] = None,
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
        if action_type == "memory_dream":
            result = self._memory_dream(task, action, None)
        elif action_type == "memory_scan_episodic":
            result = self._memory_scan_episodic(task, action, None)
        else:
            target = ChannelTarget.from_dict(action.get("target") or {})
            if not target.channel_type:
                raise ValueError(f"task {task.get('id')} has no delivery target")

            if action_type == "send_message":
                result = self._send_message(action, target)
            elif action_type == "agent_task":
                result = self._agent_task(task, action, target, None)
            elif action_type == "tool_call":
                result = self._tool_call(task, action, target, None)
            else:
                raise ValueError(f"unknown action type: {action_type!r}")
        return result

    def run_to_result(
        self,
        task: dict[str, Any],
        run_id: str = "",
        *,
        run_kind: str = "",
        scheduled_for: str = "",
        deliver_at: str = "",
    ) -> ExecutionResult:
        """Execute a scheduled action without delivering its output.

        The coordinator persists and delivers the returned result separately so
        long-running agent work cannot block the scheduling clock.
        """
        action = task.get("action") or {}
        action_type = action.get("type")
        run = {
            "id": run_id,
            "task_id": task.get("id"),
            "run_kind": run_kind,
            "scheduled_for": scheduled_for,
            "deliver_at": deliver_at,
        }
        try:
            if action_type == "send_message":
                content = (action.get("content") or "").strip()
                if not content:
                    raise ValueError("send_message action has empty content")
                result = ExecutionResult("succeeded", content=content)
            elif action_type == "memory_dream":
                self._memory_dream(task, action, None)
                result = ExecutionResult("succeeded")
            elif action_type == "memory_scan_episodic":
                self._memory_scan_episodic(task, action, None)
                result = ExecutionResult("succeeded")
            elif action_type == "agent_task":
                result = self._run_agent_to_result(task, action, None)
            elif action_type == "tool_call":
                result = self._run_tool_to_result(task, action, None)
            else:
                raise ValueError(f"unknown action type: {action_type!r}")
            return result
        except Exception as exc:
            return ExecutionResult(
                "failed",
                error=f"{type(exc).__name__}: {exc}",
            )

    def _run_agent_to_result(
        self,
        task: dict[str, Any],
        action: dict[str, Any],
        trace: Any,
    ) -> ExecutionResult:
        if self.run_agent is None or self.build_context is None:
            raise ValueError("agent_task requires run_agent and build_context")
        ai_task = (action.get("ai_task") or "").strip()
        if not ai_task:
            raise ValueError("agent_task action has empty ai_task")
        target = ChannelTarget.from_dict(action.get("target") or {})
        context = self._scheduler_context(task, target, trace=trace)
        answer, _agent_trace = self.run_agent(ai_task, context=context)
        waiting = context.get("waiting_approval") if context is not None else None
        if waiting:
            return ExecutionResult(
                WAITING_APPROVAL,
                content=(waiting.get("prompt") or answer or "").strip(),
                pending_approval_id=waiting.get("approval_id") or "",
                parked_session_id=context.session_id,
            )
        return ExecutionResult("succeeded", content=(answer or "").strip())

    def _run_tool_to_result(
        self,
        task: dict[str, Any],
        action: dict[str, Any],
        trace: Any,
    ) -> ExecutionResult:
        if self.run_tool is None or self.build_context is None:
            raise ValueError("tool_call requires run_tool and build_context")
        tool_name = (action.get("tool_name") or "").strip()
        if not tool_name:
            raise ValueError("tool_call action has empty tool_name")
        target = ChannelTarget.from_dict(action.get("target") or {})
        context = self._scheduler_context(task, target, trace=trace, resume_kind="tool_call")
        tool_result = self.run_tool(tool_name, action.get("tool_arguments") or {}, context)
        if getattr(tool_result, "waiting_approval", False):
            return ExecutionResult(
                WAITING_APPROVAL,
                content=(getattr(tool_result, "content", "") or "").strip(),
                pending_approval_id=getattr(tool_result, "approval_id", "") or "",
                parked_session_id=context.session_id,
            )
        return ExecutionResult(
            "succeeded" if bool(getattr(tool_result, "ok", True)) else "failed",
            content=(getattr(tool_result, "content", str(tool_result)) or "").strip(),
            error="" if bool(getattr(tool_result, "ok", True)) else (getattr(tool_result, "content", "") or ""),
        )

    def _memory_dream(self, task: dict[str, Any], action: dict[str, Any], trace: Any) -> bool:
        if self.run_dream is None:
            raise ValueError("memory_dream requires run_dream")

        try:
            lookback_days = int(action.get("lookback_days") or 1)
        except (TypeError, ValueError) as e:
            raise ValueError("memory_dream lookback_days must be an integer") from e
        if lookback_days <= 0:
            raise ValueError("memory_dream lookback_days must be > 0")

        result = self.run_dream(lookback_days=lookback_days)
        return True

    def _memory_scan_episodic(self, task: dict[str, Any], action: dict[str, Any], trace: Any) -> bool:
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
        trace: Any,
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
        if not answer:
            return True
        return self._deliver(target, answer)

    def _tool_call(
        self,
        task: dict[str, Any],
        action: dict[str, Any],
        target: ChannelTarget,
        trace: Any,
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
        if not (content or "").strip():
            return True
        return self._deliver(target, content)

    def _scheduler_context(self, task: dict[str, Any], target: ChannelTarget, trace: Any, **extra):
        session_id = f"scheduler:{target.session_id or target.user_id}:{task.get('id')}"
        return self.build_context(
            session_id=session_id,
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
