import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, Union

from memory import MemoryManager
from tools.approval import ApprovalGate, format_argument_lines
from tools.base import BaseTool, ToolResult
from tools.bash_tool import BashTool
from tools.file_tools import EditFileTool, GlobTool, ReadFileTool, WriteFileTool
from tools.memory_tools import ReadMemoryTool, RememberMemoryTool, SearchMemoryTool
from tools.policy import ToolDecision, ToolPolicy, ToolRequest
from tools.scheduler_tool import SchedulerTool
from tools.sandbox import DEFAULT_SANDBOX_IMAGE
from tools.spill import maybe_spill
from tools.web_search_tool import WebSearchTool
from observability.otel import add_content_event, set_attributes, set_error, start_span

try:
    from jsonschema import Draft202012Validator
except ImportError:  # pragma: no cover - requirements installation failure
    Draft202012Validator = None

if TYPE_CHECKING:
    from agent.run_context import RunContext
    from scheduler.repository import SchedulerRepository

SCHEDULED_TASK_HIDDEN_TOOLS = {"scheduler"}


class ToolRegistry:
    def __init__(
        self,
        workspace: Path,
        memory: MemoryManager,
        bash_timeout: int = 20,
        qianfan_api_key: str = "",
        qianfan_api_base: str = "",
        web_search_timeout: int = 15,
        scheduler_store: "Optional[SchedulerRepository]" = None,
        approval_mode: str = "risk_based",
        policy: Optional[ToolPolicy] = None,
        approval_gate: Optional[ApprovalGate] = None,
        sandbox_enabled: bool = True,
        sandbox_image: str = DEFAULT_SANDBOX_IMAGE,
        sandbox_network: str = "none",
        sandbox_memory: str = "512m",
        sandbox_cpus: float = 1.0,
        sandbox_pids_limit: int = 128,
        sandbox_user: str = "65532:65532",
    ):
        self.policy = policy or ToolPolicy(mode=approval_mode)
        self.approval_gate = approval_gate
        self.tools: dict[str, BaseTool] = {}
        for tool in (
            BashTool(
                workspace,
                default_timeout=bash_timeout,
                enabled=sandbox_enabled,
                image=sandbox_image,
                network=sandbox_network,
                memory=sandbox_memory,
                cpus=sandbox_cpus,
                pids_limit=sandbox_pids_limit,
                user=sandbox_user,
            ),
            GlobTool(workspace),
            ReadFileTool(workspace),
            WriteFileTool(workspace),
            EditFileTool(workspace),
            RememberMemoryTool(workspace, memory),
            SearchMemoryTool(workspace, memory),
            ReadMemoryTool(workspace, memory),
        ):
            self.tools[tool.name] = tool

        if WebSearchTool.is_available(qianfan_api_key):
            tool = WebSearchTool(
                workspace,
                api_key=qianfan_api_key,
                api_base=qianfan_api_base,
                timeout=web_search_timeout,
            )
            self.tools[tool.name] = tool

        if scheduler_store is not None:
            scheduler_tool = SchedulerTool(workspace, scheduler_store)
            self.tools[scheduler_tool.name] = scheduler_tool

    def schemas(self, context: "Optional[RunContext]" = None) -> list[dict[str, Any]]:
        hidden = self._hidden_tools(context)
        return [
            tool.schema()
            for name, tool in self.tools.items()
            if name not in hidden
        ]

    def run(
        self,
        name: str,
        arguments: Union[str, dict[str, Any]],
        context: "Optional[RunContext]" = None,
    ) -> ToolResult:
        span_context = start_span(
            f"execute_tool {name or 'unknown'}",
            attributes={
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.tool.name": name,
                "my_agent.tool.argument_chars": self._argument_chars(arguments),
            },
        )
        with span_context as span:
            if name in self._hidden_tools(context):
                result = ToolResult(
                    ok=False,
                    content=f"Tool not available in this context: {name}",
                    error_code="tool_unavailable",
                    error_type="policy_error",
                    retryable=False,
                )
                self._finish_tool_span(span, result, error_type="hidden_tool")
                return result

            tool = self.tools.get(name)
            if tool is None:
                result = ToolResult(
                    ok=False,
                    content=f"Unknown tool: {name}",
                    error_code="unknown_tool",
                    error_type="validation_error",
                    retryable=True,
                    suggested_action="Choose a tool from the provided tool definitions",
                )
                self._finish_tool_span(span, result, error_type="unknown_tool")
                return result

            if isinstance(arguments, str):
                try:
                    parsed = json.loads(arguments or "{}")
                except json.JSONDecodeError as e:
                    result = ToolResult(
                        ok=False,
                        content=f"Invalid JSON arguments: {e}",
                        error_code="invalid_arguments",
                        error_type="validation_error",
                        retryable=True,
                        details={"line": e.lineno, "column": e.colno},
                        suggested_action="Generate valid JSON arguments matching the tool schema",
                    )
                    self._finish_tool_span(span, result, error_type="invalid_json")
                    return result
            else:
                parsed = arguments

            validation_error = self._validate_arguments(tool, parsed)
            if validation_error is not None:
                self._finish_tool_span(span, validation_error, error_type="invalid_arguments")
                return validation_error

            if span is not None:
                set_attributes(
                    span,
                    **{"my_agent.tool.argument_keys": sorted(parsed.keys()) if isinstance(parsed, dict) else []},
                )
                add_content_event(span, "my_agent.tool.input", parsed)

            request = self._build_request(tool, parsed, context)
            decision = self._apply_policy(tool, request, parsed, context, span)
            if decision.action != "allow":
                result = self._denied_result(request, decision)
                self._finish_tool_span(span, result, error_type=self._error_type(decision))
                return result

            result = tool.run(parsed, context)
            if (
                context is not None
                and result.ok
                and result.metadata.get("trust_level") == "untrusted"
            ):
                context.add_metadata(untrusted_input=True)
            if result.ok:
                session_id = context.session_id if context is not None else ""
                result = maybe_spill(
                    result,
                    tool_name=name,
                    workspace=tool.workspace,
                    session_id=session_id,
                )
            self._finish_tool_span(span, result)
            return result

    def _build_request(
        self,
        tool: BaseTool,
        parsed: Any,
        context: "Optional[RunContext]",
    ) -> ToolRequest:
        arguments = parsed if isinstance(parsed, dict) else {}
        session_id = ""
        channel_type = ""
        user_id = ""
        is_scheduled_task = False
        task_id = ""
        if context is not None:
            session_id = context.session_id or ""
            channel_type = str(context.get("channel_type") or "")
            user_id = str(context.get("user_id") or "")
            is_scheduled_task = bool(context.get("is_scheduled_task"))
            task_id = str(context.get("task_id") or "")
        return ToolRequest(
            name=tool.name,
            arguments=arguments,
            risk_level=tool.risk_for(arguments),
            session_id=session_id,
            channel_type=channel_type,
            user_id=user_id,
            is_scheduled_task=is_scheduled_task,
            task_id=task_id,
            requires_approval=tool.requires_approval,
            untrusted_input=bool(context.get("untrusted_input")) if context is not None else False,
        )

    def _apply_policy(
        self,
        tool: BaseTool,
        request: ToolRequest,
        parsed: Any,
        context: "Optional[RunContext]",
        execute_span: Any,
    ) -> ToolDecision:
        span_context = start_span(
            "tool.policy",
            attributes={
                "gen_ai.tool.name": request.name,
                "my_agent.tool.risk_level": request.risk_level,
                "my_agent.policy.mode": self.policy.mode,
            },
        )
        with span_context as policy_span:
            decision = self._resolve_approval(self.policy.decide(request), request, context)
            if (
                decision.action == "allow"
                and request.risk_level in {"R1", "L1"}
            ):
                preview = self._l1_preview(tool, parsed)
                if preview is not None:
                    if policy_span is not None:
                        add_content_event(policy_span, "my_agent.tool.preview", preview)
                    if execute_span is not None:
                        add_content_event(execute_span, "my_agent.tool.preview", preview)
            if policy_span is not None:
                set_attributes(
                    policy_span,
                    **{
                        "my_agent.policy.decision": decision.action,
                        "my_agent.tool.risk_level": decision.risk_level,
                        "my_agent.policy.reason": decision.reason,
                        "my_agent.approval.id": decision.approval_id,
                        "my_agent.approval.scope": decision.scope,
                    },
                )
                if decision.action == "deny":
                    set_error(policy_span, decision.reason or "tool denied")
            return decision

    def _resolve_approval(
        self,
        decision: ToolDecision,
        request: ToolRequest,
        context: "Optional[RunContext]",
    ) -> ToolDecision:
        if decision.action != "need_approval":
            return decision
        gate = self.approval_gate
        resume_id = ""
        if context is not None:
            resume_id = str(context.get("resume_approval_id") or "")
        if gate is not None and resume_id and gate.allows_resume_request(resume_id, request):
            pending = gate.get(resume_id)
            return ToolDecision(
                action="allow",
                reason="approved",
                risk_level=decision.risk_level,
                scope=(pending.scope if pending is not None else "once"),
                approval_id=resume_id,
                approver_user_id=(pending.approver_user_id if pending is not None else ""),
            )
        if (
            gate is not None
            and self.policy.mode == "always"
            and request.name != "bash"
            and gate.is_session_allowed(request.session_id, request.name)
        ):
            return ToolDecision(
                action="allow",
                reason="session_allow",
                risk_level=decision.risk_level,
                scope="session",
            )
        if gate is None:
            return decision
        if request.is_scheduled_task:
            pending = gate.submit(
                request,
                kind="scheduled",
                resume_kind=self._resume_kind(context),
                target=self._target_from_context(context),
            )
            return ToolDecision(
                action="need_approval",
                reason="scheduled_park",
                risk_level=decision.risk_level,
                approval_id=pending.approval_id,
            )
        pending = gate.submit(request)
        self._emit_approval_request(context, pending)
        finished = gate.wait(pending.approval_id)
        gate.forget(pending.approval_id)
        if finished.status == "approved":
            return ToolDecision(
                action="allow",
                reason="approved",
                risk_level=decision.risk_level,
                scope=finished.scope,
                approval_id=finished.approval_id,
                approver_user_id=finished.approver_user_id,
            )
        if finished.status == "timeout":
            return ToolDecision(
                action="need_approval",
                reason="approval_timeout",
                risk_level=decision.risk_level,
                approval_id=finished.approval_id,
            )
        return ToolDecision(
            action="need_approval",
            reason="approval_denied",
            risk_level=decision.risk_level,
            approval_id=finished.approval_id,
            approver_user_id=finished.approver_user_id,
        )

    @staticmethod
    def _resume_kind(context: "Optional[RunContext]") -> str:
        if context is None:
            return "agent_loop"
        kind = str(context.get("resume_kind") or "")
        return "tool_call" if kind == "tool_call" else "agent_loop"

    @staticmethod
    def _target_from_context(context: "Optional[RunContext]") -> dict[str, Any]:
        if context is None:
            return {}
        return {
            "channel_type": str(context.get("channel_type") or ""),
            "user_id": str(context.get("user_id") or ""),
            "is_group": bool(context.get("is_group", False)),
            "group_id": context.get("group_id"),
            "session_id": context.session_id,
        }

    def _emit_approval_request(self, context: "Optional[RunContext]", pending: Any) -> None:
        on_event = context.get("on_event") if context is not None else None
        event = {
            "type": "approval_request",
            "content": pending.prompt,
            "approval_id": pending.approval_id,
            "tool_name": pending.request.name,
            "risk_level": pending.request.risk_level,
            # This is already redacted/truncated by the same formatter used by
            # the text fallback. Channels must never receive the raw argument
            # mapping because it may contain credentials.
            "argument_lines": format_argument_lines(pending.request.arguments),
            "timeout_seconds": (
                self.approval_gate.timeout_seconds if self.approval_gate is not None else 120
            ),
            "session_scope_allowed": pending.request.risk_level not in {"R2", "L2"},
        }
        if not callable(on_event):
            return
        try:
            on_event(event)
        except Exception:
            pass

    @staticmethod
    def _l1_preview(tool: BaseTool, parsed: Any) -> Optional[dict[str, Any]]:
        preview_fn = getattr(tool, "trace_preview", None)
        if not callable(preview_fn):
            return None
        arguments = parsed if isinstance(parsed, dict) else {}
        try:
            preview = preview_fn(arguments)
        except Exception:
            return None
        return preview if isinstance(preview, dict) else None

    @staticmethod
    def _error_type(decision: ToolDecision) -> str:
        if decision.reason == "approval_timeout":
            return "approval_timeout"
        if decision.reason == "approval_denied":
            return "approval_denied"
        if decision.reason == "scheduled_park":
            return "waiting_approval"
        if decision.action == "need_approval":
            return "approval_required"
        return "policy_denied"

    def _denied_result(self, request: ToolRequest, decision: ToolDecision) -> ToolResult:
        if decision.action == "need_approval":
            if decision.reason == "approval_timeout":
                content = (
                    f"Tool approval timed out: {request.name} "
                    f"(risk={decision.risk_level}, id={decision.approval_id})"
                )
            elif decision.reason == "approval_denied":
                content = (
                    f"Tool approval denied: {request.name} "
                    f"(risk={decision.risk_level}, id={decision.approval_id})"
                )
            elif decision.reason == "scheduled_park":
                content = ""
                if self.approval_gate is not None:
                    pending = self.approval_gate.get(decision.approval_id)
                    if pending is not None:
                        content = pending.prompt
                if not content:
                    content = (
                        f"Tool requires approval: {request.name} "
                        f"(risk={decision.risk_level})"
                    )
                return ToolResult(
                    ok=False,
                    content=content,
                    waiting_approval=True,
                    approval_id=decision.approval_id,
                    error_code="approval_required",
                    error_type="approval",
                    retryable=False,
                )
            elif request.is_scheduled_task:
                content = (
                    f"Tool requires approval: {request.name} "
                    f"(risk={decision.risk_level}); scheduled tasks cannot wait interactively"
                )
            else:
                content = (
                    f"Tool requires approval: {request.name} "
                    f"(risk={decision.risk_level}); approval channel is not available"
                )
        else:
            content = (
                f"Tool denied by policy: {request.name} "
                f"(risk={decision.risk_level}, reason={decision.reason})"
            )
        error_code = self._error_type(decision)
        return ToolResult(
            ok=False,
            content=content,
            error_code=error_code,
            error_type="permission_error",
            retryable=False,
            details={"risk_level": decision.risk_level, "reason": decision.reason},
            suggested_action="Explain that the tool was not executed",
        )

    @staticmethod
    def _validate_arguments(tool: BaseTool, parsed: Any) -> Optional[ToolResult]:
        if not isinstance(parsed, dict):
            return ToolResult(
                ok=False,
                content="Tool arguments must be a JSON object",
                error_code="invalid_arguments",
                error_type="validation_error",
                retryable=True,
                details={"received_type": type(parsed).__name__},
                suggested_action="Generate a JSON object matching the tool schema",
            )
        if Draft202012Validator is None:
            missing = [name for name in tool.parameters.get("required", []) if name not in parsed]
            error_details = [
                {"path": f"$.{name}", "message": "required property is missing"}
                for name in missing
            ]
        else:
            validator = Draft202012Validator(tool.parameters)
            error_details = []
            top_level_errors = list(validator.iter_errors(parsed))

            def leaf_errors(error):
                if error.context:
                    for child in error.context:
                        yield from leaf_errors(child)
                    return
                yield error

            flattened = [
                leaf
                for error in top_level_errors
                for leaf in leaf_errors(error)
            ]
            seen = set()
            for error in sorted(flattened, key=lambda item: list(item.absolute_path)):
                path = "$" + "".join(
                    f"[{part}]" if isinstance(part, int) else f".{part}"
                    for part in error.absolute_path
                )
                detail = {
                    "path": path,
                    "message": error.message,
                    "validator": str(error.validator or ""),
                }
                key = (detail["path"], detail["message"], detail["validator"])
                if key not in seen:
                    error_details.append(detail)
                    seen.add(key)
        if not error_details:
            return None
        errors = [f"{item['path']}: {item['message']}" for item in error_details]
        return ToolResult(
            ok=False,
            content="; ".join(errors),
            error_code="invalid_arguments",
            error_type="validation_error",
            retryable=True,
            details={"schema_errors": error_details},
            suggested_action="Generate corrected arguments matching the tool JSON Schema",
        )

    @staticmethod
    def _hidden_tools(context: "Optional[RunContext]") -> set[str]:
        if context is not None and context.get("is_scheduled_task"):
            return SCHEDULED_TASK_HIDDEN_TOOLS
        return set()

    @staticmethod
    def _argument_chars(arguments: Union[str, dict[str, Any]]) -> int:
        if isinstance(arguments, str):
            return len(arguments)
        return len(json.dumps(arguments, ensure_ascii=False, default=str))

    @staticmethod
    def _finish_tool_span(span: Any, result: ToolResult, error_type: str = "") -> None:
        if span is None:
            return
        set_attributes(
            span,
            **{
                "my_agent.tool.ok": result.ok,
                "my_agent.tool.result_chars": len(result.content or ""),
                "error.type": result.error_type or error_type,
                "my_agent.error.code": result.error_code,
                "my_agent.error.retryable": result.retryable,
            },
        )
        add_content_event(span, "my_agent.tool.output", result.function_output("", ""))
        if result.metadata:
            set_attributes(span, **result.metadata)
        if error_type:
            span.set_attribute("error.type", error_type)
        if not result.ok:
            set_error(span, result.content or error_type or "tool failed")
