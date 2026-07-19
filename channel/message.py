from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class IncomingMessage:
    channel_type: str
    user_id: str
    content: str
    msg_id: str
    is_group: bool = False
    group_id: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def session_id(self) -> str:
        scope = self.group_id if self.is_group and self.group_id else self.user_id
        return f"{self.channel_type}:{scope}"
