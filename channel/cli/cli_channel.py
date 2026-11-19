import uuid
from typing import Any

from channel.chat_channel import ChatChannel
from channel.delivery import DeliveryReceipt
from channel.message import IncomingMessage
from channel.reply import Reply
from observability.otel import add_content_event, add_event, current_context, get_tracer, set_attributes
from tools.approval import handle_approval_command

STOP = "stop"
CONTINUE = "continue"
PRODUCED = "produced"


class _CLIStreamHandle:
    """Write visible Responses deltas immediately and avoid a duplicate final answer."""

    def __init__(self):
        self._parts: list[str] = []
        self._started = False
        self._last_delta_type = ""
        self._needs_separator = False
        self._delta_count = 0
        self._parent_context = current_context()
        self._span = None

    def _ensure_span(self, source: str):
        if self._span is not None:
            return self._span
        self._span = get_tracer().start_span(
            "send_response cli",
            context=self._parent_context,
        )
        set_attributes(
            self._span,
            **{
                "messaging.system": "cli",
                "my_agent.channel.delivery_mode": "direct_delta",
                "my_agent.channel.first_output_source": source,
            },
        )
        return self._span

    def publish(self, event: dict[str, Any]) -> bool:
        event_type = str(event.get("type") or "")
        if event_type == "assistant_progress":
            # The same model-authored pre-tool text has already arrived through
            # output_text deltas. Consume the summary event without printing it
            # a second time, and separate the next model response visually.
            self._needs_separator = True
            return True
        if event_type not in {"assistant_delta", "assistant_progress_delta"}:
            return False
        content = str(event.get("content") or "")
        if not content:
            return True
        span = self._ensure_span(event_type)
        if self._delta_count == 0:
            add_event(
                span,
                "my_agent.channel.first_chunk_buffered",
                {
                    "my_agent.channel.chunk_type": event_type,
                    "my_agent.channel.chunk_chars": len(content),
                },
            )
        if not self._started:
            print()
            self._started = True
        elif self._needs_separator or (
            self._last_delta_type and self._last_delta_type != event_type
        ):
            print()
        print(content, end="", flush=True)
        self._parts.append(content)
        self._last_delta_type = event_type
        self._needs_separator = False
        self._delta_count += 1
        return True

    def complete(self, final_text: str, timeout: float = 15.0) -> DeliveryReceipt:
        del timeout
        span = self._ensure_span("final_response")
        emitted = "".join(self._parts)
        if not self._started:
            print(f"\n{final_text or ''}")
        elif final_text and not emitted.endswith(final_text):
            # Compatibility fallback for providers that stop emitting deltas
            # before the completed response body. Prefer a complete answer even
            # if that means printing it on a new line.
            print(f"\n{final_text}")
        else:
            print()
        add_event(
            span,
            "my_agent.channel.final_chunk_buffered",
            {"my_agent.channel.final_chars": len(final_text or "")},
        )
        add_event(
            span,
            "my_agent.channel.flush",
            {
                "final": True,
                "chars": len(final_text or ""),
                "delivered": True,
                "status": "delivered",
                "my_agent.channel.delivery_mode": "direct_delta",
                "my_agent.channel.delta_count": self._delta_count,
            },
        )
        add_content_event(span, "my_agent.channel.final_content", final_text or "")
        set_attributes(
            span,
            **{
                "my_agent.channel.delivered": True,
                "my_agent.channel.final_chars": len(final_text or ""),
                "my_agent.channel.delta_count": self._delta_count,
            },
        )
        span.end()
        return DeliveryReceipt.success()


class CLIChannel(ChatChannel):
    channel_type = "cli"
    user_id = "local"
    deliver_assistant_progress = False

    def startup(self) -> None:  # pragma: no cover - interactive stdin loop, excluded from offline tests
        self.start_consuming()
        print("my-agent CLI")
        print(f"workspace: {self.settings.workspace}")
        print("Type 'exit' to quit.")
        while True:
            try:
                line = input("\n> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if self._dispatch(line) == STOP:
                break

    def _dispatch(self, line: str) -> str:
        if not line:
            return CONTINUE
        if line.lower() in {"exit", "quit"}:
            return STOP
        if line == "/dream":
            result = self.agent.memory.deep_dream(lookback_days=1)
            if result.get("ok"):
                print(f"dream done. diary: {result.get('dream_path') or '(no diary)'}")
            else:
                print(f"dream skipped: {result.get('reason', 'unknown')}")
            return CONTINUE
        if line == "/new":
            cid = self.start_new_conversation(self.user_id)
            print(f"🌱 new conversation: {cid}")
            return CONTINUE
        if line == "/summarize":
            summarize = getattr(self.agent, "summarize_session", None)
            if callable(summarize):
                cid = self.current_conversation(self.user_id)
                print(summarize(f"{self.channel_type}:{self.user_id}:{cid}"))
            else:
                print("无需压缩")
            return CONTINUE
        handled = handle_approval_command(self.agent, line, self.user_id)
        if handled is not None:
            print(handled)
            return CONTINUE
        if line == "/sop" or line.startswith("/sop "):
            days = self._parse_sop_days(line)
            result = self.agent.memory.distill_workflows(lookback_days=days)
            if result.get("ok"):
                print(f"sop distilled (last {days}d). file: {result.get('sop_path')}")
                insights = (result.get("insights") or "").strip()
                if insights:
                    print(f"\ninsights:\n{insights}")
            else:
                print(f"sop skipped: {result.get('reason', 'unknown')}")
            return CONTINUE

        self.produce(IncomingMessage(
            channel_type=self.channel_type,
            user_id=self.user_id,
            content=line,
            msg_id=uuid.uuid4().hex,
            conversation_id=self.current_conversation(self.user_id),
        ))
        return PRODUCED

    def send(self, reply: Reply, message: IncomingMessage) -> DeliveryReceipt:
        print(f"\n{reply.content}")
        return DeliveryReceipt.success()

    def _open_stream(self, message: IncomingMessage) -> _CLIStreamHandle:
        del message
        return _CLIStreamHandle()

    @staticmethod
    def _parse_sop_days(line: str, default: int = 3) -> int:
        parts = line.split()
        if len(parts) < 2:
            return default
        try:
            days = int(parts[1])
        except ValueError:
            return default
        return days if days > 0 else default
