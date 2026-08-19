import json
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from tools.policy import ToolRequest

DEFAULT_TIMEOUT_SECONDS = 120.0
DEFAULT_SCHEDULED_TIMEOUT_SECONDS = 24 * 3600
LONG_TEXT_KEYS = {"content", "new_text", "old_text"}
LONG_TEXT_LIMIT = 200
APPROVAL_USAGE = "用法: /approve <id> [session] 或 /deny <id>"
_SENSITIVE_KEY_PARTS = ("api_key", "password", "authorization", "secret")
_SENSITIVE_VALUE_RE = re.compile(
    r"(?i)((?:api_key|password|secret|authorization|\btoken\b)\s*[:=]|Bearer\s+\S+)"
)


@dataclass
class PendingApproval:
    approval_id: str
    request: ToolRequest
    event: threading.Event
    created_at: float
    status: str = "pending"
    scope: str = "once"
    approver_user_id: str = ""
    prompt: str = ""
    kind: str = "interactive"
    resume_kind: str = "agent_loop"
    tool_call_id: str = ""
    remaining_tool_calls: list = field(default_factory=list)
    target: dict = field(default_factory=dict)


class ApprovalGate:
    def __init__(
        self,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        scheduled_timeout_seconds: float = DEFAULT_SCHEDULED_TIMEOUT_SECONDS,
    ):
        self.timeout_seconds = float(timeout_seconds)
        self.scheduled_timeout_seconds = float(scheduled_timeout_seconds)
        self.on_scheduled_resolved: Optional[Callable[[PendingApproval, str], None]] = None
        self._lock = threading.Lock()
        self._pending: dict[str, PendingApproval] = {}
        self._session_allow: set[tuple[str, str]] = set()

    def is_session_allowed(self, session_id: str, tool_name: str) -> bool:
        if not session_id:
            return False
        with self._lock:
            return (session_id, tool_name) in self._session_allow

    def submit(
        self,
        request: ToolRequest,
        *,
        kind: str = "interactive",
        resume_kind: str = "agent_loop",
        target: Optional[dict[str, Any]] = None,
        tool_call_id: str = "",
    ) -> PendingApproval:
        kind = "scheduled" if kind == "scheduled" else "interactive"
        prompt_timeout = (
            self.scheduled_timeout_seconds if kind == "scheduled" else self.timeout_seconds
        )
        with self._lock:
            approval_id = self._new_id_locked()
            pending = PendingApproval(
                approval_id=approval_id,
                request=request,
                event=threading.Event(),
                created_at=time.monotonic(),
                kind=kind,
                resume_kind="tool_call" if resume_kind == "tool_call" else "agent_loop",
                tool_call_id=str(tool_call_id or ""),
                target=dict(target or {}),
            )
            pending.prompt = format_approval_prompt(
                approval_id,
                request,
                timeout_seconds=prompt_timeout,
            )
            self._pending[approval_id] = pending
            return pending

    def wait(self, approval_id: str) -> PendingApproval:
        with self._lock:
            pending = self._pending.get(approval_id)
        if pending is None:
            raise KeyError(approval_id)
        remaining = self.timeout_seconds - (time.monotonic() - pending.created_at)
        pending.event.wait(timeout=max(0.0, remaining))
        with self._lock:
            if pending.status == "pending":
                pending.status = "timeout"
            return pending

    def approve(
        self,
        approval_id: str,
        user_id: str,
        scope: str = "once",
    ) -> tuple[bool, str]:
        fire_pending = None
        with self._lock:
            pending, error = self._validate_actor_locked(approval_id, user_id)
            if error:
                return False, error
            resolved_scope = "session" if str(scope).strip().lower() == "session" else "once"
            pending.status = "approved"
            pending.scope = resolved_scope
            pending.approver_user_id = user_id
            if resolved_scope == "session" and pending.request.session_id:
                self._session_allow.add((pending.request.session_id, pending.request.name))
            pending.event.set()
            if pending.kind == "scheduled":
                fire_pending = pending
            if resolved_scope == "session":
                message = f"已批准 [{approval_id}]（本会话允许同类工具）"
            else:
                message = f"已批准 [{approval_id}]（仅本次）"
        if fire_pending is not None:
            self._fire_scheduled_resolved(fire_pending, "approved")
        return True, message

    def deny(self, approval_id: str, user_id: str) -> tuple[bool, str]:
        fire_pending = None
        with self._lock:
            pending, error = self._validate_actor_locked(approval_id, user_id)
            if error:
                return False, error
            pending.status = "denied"
            pending.approver_user_id = user_id
            pending.event.set()
            if pending.kind == "scheduled":
                fire_pending = pending
        if fire_pending is not None:
            self._fire_scheduled_resolved(fire_pending, "denied")
        return True, f"已拒绝 [{approval_id}]"

    def pending_ids(self) -> list[str]:
        with self._lock:
            return [
                approval_id
                for approval_id, pending in self._pending.items()
                if pending.status == "pending"
            ]

    def get(self, approval_id: str) -> Optional[PendingApproval]:
        with self._lock:
            return self._pending.get(approval_id)

    def allows_resume(self, approval_id: str, tool_name: str) -> bool:
        pending = self.get(approval_id)
        return (
            pending is not None
            and pending.status == "approved"
            and pending.request.name == tool_name
        )

    def attach_resume_state(
        self,
        approval_id: str,
        *,
        tool_call_id: str = "",
        remaining_tool_calls: Optional[list] = None,
        resume_kind: Optional[str] = None,
        target: Optional[dict[str, Any]] = None,
    ) -> None:
        with self._lock:
            pending = self._pending.get(approval_id)
            if pending is None:
                return
            if tool_call_id:
                pending.tool_call_id = str(tool_call_id)
            if remaining_tool_calls is not None:
                pending.remaining_tool_calls = list(remaining_tool_calls)
            if resume_kind:
                pending.resume_kind = "tool_call" if resume_kind == "tool_call" else "agent_loop"
            if target is not None:
                pending.target = dict(target)

    def expire_scheduled(self, now_monotonic: Optional[float] = None) -> list[PendingApproval]:
        now = time.monotonic() if now_monotonic is None else now_monotonic
        expired: list[PendingApproval] = []
        with self._lock:
            for pending in self._pending.values():
                if pending.kind != "scheduled" or pending.status != "pending":
                    continue
                if now - pending.created_at >= self.scheduled_timeout_seconds:
                    pending.status = "timeout"
                    expired.append(pending)
        for pending in expired:
            self._fire_scheduled_resolved(pending, "timeout")
        return expired

    def forget(self, approval_id: str) -> None:
        with self._lock:
            self._pending.pop(approval_id, None)

    def _fire_scheduled_resolved(self, pending: PendingApproval, outcome: str) -> None:
        callback = self.on_scheduled_resolved
        if pending.kind != "scheduled" or not callable(callback):
            return
        try:
            callback(pending, outcome)
        except Exception:
            pass

    def _validate_actor_locked(
        self,
        approval_id: str,
        user_id: str,
    ) -> tuple[Optional[PendingApproval], str]:
        pending = self._pending.get(approval_id)
        if pending is None:
            return None, f"未找到待审批操作 [{approval_id}]"
        if pending.status != "pending":
            if pending.status == "timeout":
                return None, "审批已超时"
            return None, "该审批已处理"
        timeout = (
            self.scheduled_timeout_seconds
            if pending.kind == "scheduled"
            else self.timeout_seconds
        )
        if (time.monotonic() - pending.created_at) >= timeout:
            pending.status = "timeout"
            pending.event.set()
            return None, "审批已超时"
        if pending.request.user_id and user_id != pending.request.user_id:
            return None, "无权审批该操作"
        return pending, ""

    def _new_id_locked(self) -> str:
        for _ in range(8):
            approval_id = uuid.uuid4().hex[:8]
            if approval_id not in self._pending:
                return approval_id
        return uuid.uuid4().hex[:8]


def parse_approval_command(text: str) -> Optional[tuple[str, str, str]]:
    stripped = (text or "").strip()
    if not stripped:
        return None
    parts = stripped.split()
    command = parts[0].lower()
    if command not in {"/approve", "/deny"}:
        return None
    if len(parts) < 2 or not parts[1]:
        return ("invalid", "", "")
    approval_id = parts[1]
    if command == "/deny":
        return ("deny", approval_id, "")
    scope = "session" if len(parts) >= 3 and parts[2].lower() == "session" else "once"
    return ("approve", approval_id, scope)


def handle_approval_command(agent: Any, text: str, user_id: str) -> Optional[str]:
    parsed = parse_approval_command(text)
    if parsed is None:
        return None
    action, approval_id, scope = parsed
    if action == "invalid":
        return APPROVAL_USAGE
    if action == "approve":
        approve = getattr(agent, "approve_tool", None)
        if not callable(approve):
            return "审批通道不可用"
        return approve(approval_id, user_id, scope)
    deny = getattr(agent, "deny_tool", None)
    if not callable(deny):
        return "审批通道不可用"
    return deny(approval_id, user_id)


def format_approval_prompt(
    approval_id: str,
    request: ToolRequest,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> str:
    timeout_text = _format_timeout(timeout_seconds)
    argument_lines = format_argument_lines(request.arguments)
    return (
        f"待审批 [{approval_id}]\n"
        f"工具: {request.name}    等级: {request.risk_level}\n"
        f"参数:\n"
        f"{argument_lines}\n"
        f"\n"
        f"/approve {approval_id}          仅本次\n"
        f"/approve {approval_id} session  本会话允许同类工具\n"
        f"/deny {approval_id}             拒绝\n"
        f"超时 {timeout_text} 将自动拒绝。"
    )


def format_argument_lines(arguments: dict[str, Any]) -> str:
    if not arguments:
        return "  (none)"
    lines = []
    for key, value in arguments.items():
        lines.append(f"  {key}: {_format_arg_value(str(key), value)}")
    return "\n".join(lines)


def _format_arg_value(key: str, value: Any) -> str:
    if _is_sensitive_arg_key(key) or _value_looks_sensitive(value):
        return "[redacted]"
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, default=str)
    if key in LONG_TEXT_KEYS and len(text) > LONG_TEXT_LIMIT:
        return f"{text[:LONG_TEXT_LIMIT]}（共 {len(text)} 字）"
    return text


def _is_sensitive_arg_key(key: str) -> bool:
    normalized = (key or "").lower().replace("-", "_")
    if normalized == "token" or normalized.endswith("_token") or normalized.endswith("_secret"):
        return True
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _value_looks_sensitive(value: Any) -> bool:
    if isinstance(value, dict):
        text = json.dumps(value, ensure_ascii=False, default=str)
    elif isinstance(value, str):
        text = value
    else:
        return False
    return _SENSITIVE_VALUE_RE.search(text) is not None


def _format_timeout(timeout_seconds: float) -> str:
    seconds = float(timeout_seconds)
    if abs(seconds - DEFAULT_SCHEDULED_TIMEOUT_SECONDS) < 0.1:
        return "24h"
    if seconds == int(seconds):
        return f"{int(seconds)}s"
    return f"{timeout_seconds}s"
