import json
import threading
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from agent.agent import Agent
from agent.run_context import RunContext, TurnResult
from channel.base import Channel
from channel.conversation import new_conversation_id
from channel.message import IncomingMessage
from channel.reply import Reply
from config import Settings
from observability.otel import add_content_event, add_event, record_error, set_attributes, set_error, stable_hash, start_span, trace_id_hex
from state.database import StateDatabase

_CONVERSATION_STATE_LOCK = threading.Lock()


class ChatChannel(Channel):
    """Channel-agnostic scheduling: per-session serial, cross-session parallel.

    Each session gets its own queue + BoundedSemaphore(1), so only one message
    per session is in flight at a time. Different sessions run concurrently on a
    shared executor. This mirrors CowAgent's chat_channel dispatch model.
    """

    # Channels without their own stream handle may surface a model-authored
    # progress sentence before tools run. Stream-capable channels consume the
    # event themselves; approval requests remain interactive on every channel.
    deliver_assistant_progress = True

    def __init__(
        self,
        agent: Agent,
        settings: Settings,
        executor: Optional[ThreadPoolExecutor] = None,
        poll_interval: float = 0.05,
        max_workers: int = 8,
    ):
        self.agent = agent
        self.settings = settings
        self.poll_interval = poll_interval
        self._owns_executor = executor is None
        self.executor = executor or ThreadPoolExecutor(max_workers=max_workers)
        self.sessions: dict[str, list] = {}
        self.lock = threading.Lock()
        self._running = False
        self._consume_thread: Optional[threading.Thread] = None
        self._state_database = getattr(agent, "state_database", None)
        if self._state_database is None:
            state_path = settings.state_db_path
            if not state_path.is_absolute():
                state_path = settings.project_root / state_path
            self._state_database = StateDatabase(state_path)
        self._conversations: dict[str, str] = {}
        self._conv_lock = threading.Lock()
        self._load_conversations()

    def _conversation_state_path(self) -> Path:
        return self.settings.project_root / "data" / "memory" / "conversation_state.json"

    def _conversation_key(self, scope: str) -> str:
        channel = getattr(self, "channel_type", None) or "chat"
        return f"{channel}:{scope}"

    def _load_conversations(self) -> None:
        stored = self._state_database.get_metadata("conversation_state")
        if isinstance(stored, dict):
            self._conversations = {
                str(key): str(value) for key, value in stored.items() if key and value
            }
            return

        # One-time, non-destructive import from the legacy JSON file.
        path = self._conversation_state_path()
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return
        if isinstance(data, dict):
            self._conversations = {
                str(key): str(value) for key, value in data.items() if key and value
            }
            self._state_database.set_metadata("conversation_state", self._conversations)

    def _persist_conversations(self) -> None:
        with _CONVERSATION_STATE_LOCK:
            merged = self._state_database.merge_metadata_mapping(
                "conversation_state",
                self._conversations,
            )
            self._conversations = merged

    def current_conversation(self, scope: str) -> str:
        key = self._conversation_key(scope)
        with self._conv_lock:
            cid = self._conversations.get(key)
            if cid is None:
                cid = new_conversation_id()
                self._conversations[key] = cid
                self._persist_conversations()
            return cid

    def start_new_conversation(self, scope: str) -> str:
        key = self._conversation_key(scope)
        with self._conv_lock:
            previous = self._conversations.get(key)
            cid = new_conversation_id()
            while cid == previous:
                cid = new_conversation_id()
            self._conversations[key] = cid
            self._persist_conversations()
            return cid

    def produce(self, message: IncomingMessage) -> None:
        session_id = message.session_id
        with self.lock:
            if session_id not in self.sessions:
                self.sessions[session_id] = [deque(), threading.BoundedSemaphore(1)]
            self.sessions[session_id][0].append(message)
            message.raw.setdefault("_my_agent_queue_depth", len(self.sessions[session_id][0]))
            message.raw.setdefault("_my_agent_enqueued_at_ns", time.time_ns())

    def start_consuming(self) -> threading.Thread:
        self._running = True
        thread = threading.Thread(
            target=self.consume,
            daemon=True,
            name=f"{self.channel_type or 'chat'}-consume",
        )
        thread.start()
        self._consume_thread = thread
        return thread

    def consume(self) -> None:
        while self._running:
            with self.lock:
                session_ids = list(self.sessions.keys())
            for session_id in session_ids:
                with self.lock:
                    entry = self.sessions.get(session_id)
                if entry is None:
                    continue
                queue, semaphore = entry
                if semaphore.acquire(blocking=False):
                    message = None
                    with self.lock:
                        if queue:
                            message = queue.popleft()
                    if message is not None:
                        future = self.executor.submit(self._handle, message)
                        future.add_done_callback(self._make_done_callback(session_id))
                    else:
                        semaphore.release()
            time.sleep(self.poll_interval)

    def _make_done_callback(self, session_id: str):
        def _callback(_future: Future) -> None:
            with self.lock:
                entry = self.sessions.get(session_id)
            if entry is not None:
                entry[1].release()

        return _callback

    def _handle(self, message: IncomingMessage) -> None:
        worker_started_ns = time.time_ns()
        context = RunContext.create(
            session_id=message.session_id,
            workspace=self.settings.workspace,
            entrypoint=message.channel_type,
            channel_type=message.channel_type,
            user_id=message.user_id,
            is_group=message.is_group,
            group_id=message.group_id,
            message_id=message.msg_id,
            conversation_id=message.conversation_id or "",
            received_at_ns=message.received_at_ns,
        )

        stream_handle = None

        def _on_event(event: dict) -> None:
            if stream_handle is not None and stream_handle.publish(event):
                return
            event_type = event.get("type")
            if event_type not in {"assistant_progress", "approval_request"}:
                return
            if event_type == "assistant_progress" and not self.deliver_assistant_progress:
                return
            content = str(event.get("content") or "").strip()
            if content:
                with start_span(
                    "channel.progress",
                    attributes={
                        "my_agent.session.id": message.session_id,
                        "messaging.system": message.channel_type,
                        "my_agent.reply.chars": len(content),
                        "my_agent.reply.type": event_type,
                    },
                ):
                    self._send_event(event, message)

        conversation_id = message.conversation_id or message.session_id
        platform_created_at_ns = self._platform_created_at_ns(message)
        transport_ns = message.received_at_ns - platform_created_at_ns if platform_created_at_ns else 0
        platform_time_usable = 0 <= transport_ns <= 300 * 1_000_000_000
        turn_started_at_ns = platform_created_at_ns if platform_time_usable else message.received_at_ns
        with start_span(
            "turn my-agent",
            root=True,
            start_time=turn_started_at_ns,
            attributes={
                "my_agent.turn.id": message.msg_id,
                "my_agent.session.id": message.session_id,
                "messaging.message.id": message.msg_id,
                "messaging.conversation.id": conversation_id,
                "messaging.system": message.channel_type,
                "my_agent.user.id_hash": stable_hash(message.user_id),
                "gen_ai.request.model": self.settings.model,
                "my_agent.queue.depth": message.raw.get("_my_agent_queue_depth", 0),
                "messaging.message.created_at_unix_nano": (
                    platform_created_at_ns if platform_time_usable else None
                ),
            },
        ) as turn_span:
            add_content_event(turn_span, "my_agent.user_message", message.content or "")
            try:
                enqueued_at_ns = int(message.raw.get("_my_agent_enqueued_at_ns") or message.received_at_ns)
                with start_span(
                    f"receive_request {message.channel_type}",
                    start_time=turn_started_at_ns,
                    attributes={
                        "my_agent.queue.depth": message.raw.get("_my_agent_queue_depth", 0),
                        "my_agent.queue.wait_ms": round(
                            max(0, worker_started_ns - enqueued_at_ns) / 1_000_000,
                            3,
                        ),
                        "my_agent.ingress.parse_ms": round(
                            max(0, enqueued_at_ns - message.received_at_ns) / 1_000_000,
                            3,
                        ),
                        "my_agent.ingress.total_ms": round(
                            max(0, worker_started_ns - message.received_at_ns) / 1_000_000,
                            3,
                        ),
                        "my_agent.ingress.transport_ms": (
                            round(max(0, message.received_at_ns - platform_created_at_ns) / 1_000_000, 3)
                            if platform_time_usable
                            else None
                        ),
                    },
                ) as ingress_span:
                    if platform_time_usable:
                        add_event(
                            ingress_span,
                            "my_agent.channel.platform_message_created",
                            {"messaging.message.id": message.msg_id},
                            timestamp=platform_created_at_ns,
                        )
                    add_event(
                        ingress_span,
                        "my_agent.channel.message_received",
                        {"messaging.message.id": message.msg_id},
                        timestamp=message.received_at_ns,
                    )
                    add_event(
                        ingress_span,
                        "my_agent.channel.queue_enqueued",
                        {"my_agent.queue.depth": message.raw.get("_my_agent_queue_depth", 0)},
                        timestamp=enqueued_at_ns,
                    )
                    add_event(
                        ingress_span,
                        "my_agent.channel.worker_started",
                        timestamp=worker_started_ns,
                    )
                    add_event(ingress_span, "my_agent.channel.agent_dispatched")

                stream_handle = self._open_stream(message)

                answer, result = self.agent.run(
                    message.content,
                    context=context,
                    on_event=_on_event,
                )
                if not isinstance(result, TurnResult):
                    # Keep injected/third-party Agent implementations compatible
                    # while the built-in Agent returns the typed result.
                    result = TurnResult(trace_id=trace_id_hex(turn_span))
                if stream_handle is not None:
                    receipt = stream_handle.complete(answer)
                    wait_for_delivery = getattr(stream_handle, "wait", None)
                    if callable(wait_for_delivery) and getattr(receipt, "status", "") == "queued":
                        receipt = wait_for_delivery(timeout=15.0)
                    delivered = bool(receipt)
                else:
                    with start_span(
                        f"send_response {message.channel_type}",
                        attributes={
                            "my_agent.session.id": message.session_id,
                            "messaging.system": message.channel_type,
                            "my_agent.reply.chars": len(answer or ""),
                            "my_agent.reply.type": "final",
                        },
                    ) as stream_span:
                        receipt = self.send(Reply(answer), message)
                        delivered = receipt is None or bool(receipt)
                        add_event(
                            stream_span,
                            "my_agent.channel.flush",
                            {
                                "final": True,
                                "chars": len(answer or ""),
                                "delivered": delivered,
                                "status": getattr(receipt, "status", "delivered"),
                            },
                        )
                        add_content_event(stream_span, "my_agent.channel.final_content", answer or "")
                        if not delivered:
                            set_error(stream_span, getattr(receipt, "error", "final delivery failed"))
                if not delivered:
                    result.outcome = "failed"
                if getattr(receipt, "status", "") == "queued":
                    add_event(
                        turn_span,
                        "my_agent.channel.queued",
                        {
                            "messaging.system": message.channel_type,
                            "my_agent.channel.final_chars": len(answer or ""),
                        },
                    )
                set_attributes(
                    turn_span,
                    **{
                        "my_agent.run.outcome": result.outcome,
                        "my_agent.run.recovered_error_count": result.recovered_error_count,
                        "my_agent.trace.id": result.trace_id or trace_id_hex(turn_span),
                        "my_agent.channel.delivery_status": getattr(receipt, "status", "delivered"),
                        "my_agent.channel.delivery_pending": getattr(receipt, "status", "") == "queued",
                    },
                )
                if result.outcome in {"failed", "max_steps"}:
                    set_error(turn_span, result.outcome)
            except Exception as e:
                record_error(
                    turn_span,
                    e,
                    **{
                        "my_agent.session.id": message.session_id,
                        "messaging.system": message.channel_type,
                    },
                )
                set_attributes(turn_span, **{"my_agent.run.outcome": "failed"})
                self.send(Reply(f"ERROR: {type(e).__name__}: {e}"), message)

    def _open_stream(self, message: IncomingMessage):
        return None

    @staticmethod
    def _platform_created_at_ns(message: IncomingMessage) -> int:
        raw_message = (message.raw or {}).get("message") or {}
        value = raw_message.get("create_time")
        try:
            return int(value) * 1_000_000 if value else 0
        except (TypeError, ValueError):
            return 0

    def _send_event(self, event: dict, message: IncomingMessage):
        """Deliver a structured agent event, with text as the default surface."""
        return self.send(Reply(str(event.get("content") or "")), message)

    def stop(self) -> None:
        self._running = False
        if self._owns_executor:
            self.executor.shutdown(wait=False)
