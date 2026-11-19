from dataclasses import dataclass
from typing import Any, Optional

VALID_RISK_LEVELS = ("R0", "R1", "R2")
LEGACY_RISK_LEVELS = {"L0": "R0", "L1": "R1", "L2": "R2"}


def normalize_approval_mode(raw: Any) -> str:
    value = str(raw).strip().lower()
    aliases = {"on": "always", "off": "never"}
    value = aliases.get(value, value)
    return value if value in {"always", "never", "risk_based"} else "invalid"


def normalize_risk_level(raw: Any) -> str:
    value = str(raw or "").strip().upper()
    return LEGACY_RISK_LEVELS.get(value, value if value in VALID_RISK_LEVELS else "")


@dataclass(frozen=True)
class ToolRequest:
    name: str
    arguments: dict[str, Any]
    risk_level: str
    session_id: str = ""
    channel_type: str = ""
    user_id: str = ""
    is_scheduled_task: bool = False
    task_id: str = ""
    requires_approval: Optional[bool] = None
    untrusted_input: bool = False


@dataclass(frozen=True)
class ToolDecision:
    action: str  # allow | deny | need_approval
    reason: str = ""
    risk_level: str = "R0"
    scope: str = ""
    approval_id: str = ""
    approver_user_id: str = ""


class ToolPolicy:
    def __init__(self, mode: str = "risk_based"):
        self.mode = normalize_approval_mode(mode)

    def decide(self, request: ToolRequest) -> ToolDecision:
        risk_level = normalize_risk_level(request.risk_level)
        if self.mode == "invalid":
            return ToolDecision(
                action="deny",
                reason="invalid_approval_mode",
                risk_level=risk_level or request.risk_level,
            )
        if not risk_level:
            return ToolDecision(
                action="deny",
                reason="unknown_risk_level",
                risk_level=request.risk_level or "",
            )
        if request.untrusted_input and (
            request.name in {"remember_memory", "scheduler"}
            or (request.name in {"write_file", "edit_file", "bash"} and risk_level == "R2")
        ):
            return ToolDecision(
                action="deny",
                reason="untrusted_input_to_sensitive_sink",
                risk_level=risk_level,
            )
        if self.mode == "always":
            return ToolDecision(
                action="need_approval",
                reason="approval_mode_always",
                risk_level=risk_level,
            )
        if self.mode == "risk_based" and risk_level == "R2":
            return ToolDecision(
                action="need_approval",
                reason="risk_based_r2",
                risk_level=risk_level,
            )
        return ToolDecision(
            action="allow",
            reason=("risk_based_low_risk" if self.mode == "risk_based" else "approval_mode_never"),
            risk_level=risk_level,
        )
