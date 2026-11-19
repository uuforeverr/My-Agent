from dataclasses import dataclass, field
import time
from typing import Any, Optional


@dataclass
class IncomingMessage:
    channel_type: str
    user_id: str
    content: str
    msg_id: str
    is_group: bool = False
    group_id: Optional[str] = None
    conversation_id: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)
    received_at_ns: int = field(default_factory=time.time_ns)

    @property
    def scope(self) -> str:
        return self.group_id if self.is_group and self.group_id else self.user_id

    @property
    def session_id(self) -> str:
        base = f"{self.channel_type}:{self.scope}"
        return f"{base}:{self.conversation_id}" if self.conversation_id else base
