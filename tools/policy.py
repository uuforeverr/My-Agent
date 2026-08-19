from dataclasses import dataclass
from typing import Any, Optional

VALID_RISK_LEVELS = ("L0", "L1", "L2", "L3")
APPROVAL_REQUIRED_LEVELS = {"L2", "L3"}


def normalize_approval_mode(raw: Any) -> str:
    return "on" if str(raw).strip().lower() == "on" else "off"


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


@dataclass(frozen=True)
class ToolDecision:
    action: str  # allow | deny | need_approval
    reason: str = ""
    risk_level: str = "L0"
    scope: str = ""
    approval_id: str = ""
    approver_user_id: str = ""


class ToolPolicy:
    def __init__(self, mode: str = "off"):
        self.mode = normalize_approval_mode(mode)

    def decide(self, request: ToolRequest) -> ToolDecision:
        risk_level = request.risk_level if request.risk_level in VALID_RISK_LEVELS else ""
        if self.mode == "off":
            return ToolDecision(
                action="allow",
                reason="approval_mode_off",
                risk_level=request.risk_level or "L0",
            )
        if request.requires_approval is False:
            return ToolDecision(
                action="allow",
                reason="requires_approval_false",
                risk_level=request.risk_level or "L0",
            )
        if not risk_level:
            return ToolDecision(
                action="deny",
                reason="unknown_risk_level",
                risk_level=request.risk_level or "",
            )
        if request.requires_approval is True or risk_level in APPROVAL_REQUIRED_LEVELS:
            return ToolDecision(
                action="need_approval",
                reason="risk_requires_approval",
                risk_level=risk_level,
            )
        return ToolDecision(
            action="allow",
            reason="low_risk",
            risk_level=risk_level,
        )
