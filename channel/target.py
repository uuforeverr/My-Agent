import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

from channel.message import IncomingMessage

if TYPE_CHECKING:
    from agent.run_context import RunContext


@dataclass
class ChannelTarget:
    """A durable "who to deliver to" descriptor for proactive (unprompted) sends.

    A normal reply targets the IncomingMessage it answers. A scheduled task has
    no such message when it fires, so it snapshots the delivery target at
    creation time and rebuilds a synthetic IncomingMessage to reuse the
    channel's existing ``send(reply, message)`` path.
    """

    channel_type: str
    user_id: str
    is_group: bool = False
    group_id: Optional[str] = None
    session_id: str = ""

    def to_incoming_message(self) -> IncomingMessage:
        return IncomingMessage(
            channel_type=self.channel_type,
            user_id=self.user_id,
            content="",
            msg_id=f"scheduler:{uuid.uuid4().hex}",
            is_group=self.is_group,
            group_id=self.group_id,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel_type": self.channel_type,
            "user_id": self.user_id,
            "is_group": self.is_group,
            "group_id": self.group_id,
            "session_id": self.session_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ChannelTarget":
        return cls(
            channel_type=data.get("channel_type", ""),
            user_id=data.get("user_id", ""),
            is_group=bool(data.get("is_group", False)),
            group_id=data.get("group_id"),
            session_id=data.get("session_id", ""),
        )

    @classmethod
    def from_context(cls, context: "RunContext") -> Optional["ChannelTarget"]:
        channel_type = context.get("channel_type")
        user_id = context.get("user_id")
        if not channel_type or not user_id:
            return None
        return cls(
            channel_type=str(channel_type),
            user_id=str(user_id),
            is_group=bool(context.get("is_group", False)),
            group_id=context.get("group_id"),
            session_id=context.session_id,
        )
