import threading
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Optional

from agent.agent import Agent
from agent.run_context import RunContext
from channel.base import Channel
from channel.message import IncomingMessage
from channel.reply import Reply
from config import Settings
from observability import JsonlTraceStore, Trace


class ChatChannel(Channel):
    """Channel-agnostic scheduling: per-session serial, cross-session parallel.

    Each session gets its own queue + BoundedSemaphore(1), so only one message
    per session is in flight at a time. Different sessions run concurrently on a
    shared executor. This mirrors CowAgent's chat_channel dispatch model.
    """

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
        self.last_trace = None
        self.trace_store = JsonlTraceStore.for_project(settings.project_root)

    def produce(self, message: IncomingMessage) -> None:
        session_id = message.session_id
        with self.lock:
            if session_id not in self.sessions:
                self.sessions[session_id] = [deque(), threading.BoundedSemaphore(1)]
            self.sessions[session_id][0].append(message)

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
        context = RunContext.create(
            session_id=message.session_id,
            workspace=self.settings.workspace,
            entrypoint=message.channel_type,
            channel_type=message.channel_type,
            user_id=message.user_id,
            is_group=message.is_group,
            group_id=message.group_id,
        )

        def _on_event(event: dict) -> None:
            event_type = event.get("type")
            if event_type not in {"assistant_progress", "approval_request"}:
                return
            content = str(event.get("content") or "").strip()
            if content:
                with context.trace.start_span(
                    "channel.send",
                    span_kind="channel.send",
                    session_id=message.session_id,
                    channel_type=message.channel_type,
                    reply_chars=len(content),
                    reply_type=event_type,
                ):
                    self.send(Reply(content), message)

        try:
            with context.trace.start_span(
                "channel.handle",
                span_kind="channel.handle",
                session_id=message.session_id,
                channel_type=message.channel_type,
                user_id=message.user_id,
                message_chars=len(message.content or ""),
            ):
                answer, trace = self.agent.run(
                    message.content,
                    context=context,
                    on_event=_on_event,
                )
                self.last_trace = trace
                with context.trace.start_span(
                    "channel.send",
                    span_kind="channel.send",
                    session_id=message.session_id,
                    channel_type=message.channel_type,
                    reply_chars=len(answer or ""),
                    reply_type="final",
                ):
                    self.send(Reply(answer), message)
            self._persist_trace(trace)
        except Exception as e:
            context.trace.record_error(
                e,
                event_name="channel_handle_error",
                session_id=message.session_id,
                channel_type=message.channel_type,
                user_id=message.user_id,
            )
            self.last_trace = context.trace
            self._persist_trace(context.trace)
            self.send(Reply(f"ERROR: {type(e).__name__}: {e}"), message)

    def stop(self) -> None:
        self._running = False
        if self._owns_executor:
            self.executor.shutdown(wait=False)

    def _persist_trace(self, trace) -> None:
        if not isinstance(trace, Trace):
            return
        try:
            self.trace_store.append(trace)
        except Exception:
            pass
