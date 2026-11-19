from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from channel.delivery import DeliveryReceipt
from observability.otel import (
    add_content_event,
    add_event,
    attach_context,
    current_context,
    detach_context,
    get_tracer,
    set_attributes,
    set_error,
)


FlushCallback = Callable[[str, str, bool], DeliveryReceipt]


@dataclass
class _StreamState:
    callback: FlushCallback
    parent_context: Any
    channel_type: str
    interval: float
    char_threshold: int
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    lock: threading.Lock = field(default_factory=threading.Lock)
    text_parts: list[str] = field(default_factory=list)
    progress_parts: list[str] = field(default_factory=list)
    final_text: str = ""
    provider_message_id: str = ""
    last_flushed_text: str = ""
    last_flush_at: float = field(default_factory=time.monotonic)
    done: bool = False
    completed: threading.Event = field(default_factory=threading.Event)
    receipt: DeliveryReceipt = field(default_factory=DeliveryReceipt.success)
    span: Any = None
    flush_count: int = 0
    buffered_chunk_count: int = 0

    def visible_text(self) -> str:
        if self.done:
            return self.final_text
        text = "".join(self.text_parts)
        if text:
            return text
        return "".join(self.progress_parts)


class StreamHandle:
    def __init__(self, state: _StreamState, wake: threading.Event):
        self._state = state
        self._wake = wake

    def publish(self, event: dict[str, Any]) -> bool:
        event_type = str(event.get("type") or "")
        content = str(event.get("content") or "")
        if event_type not in {"assistant_delta", "assistant_progress_delta", "assistant_progress"}:
            return False
        if not content:
            return True
        with self._state.lock:
            if self._state.done:
                return True
            self._ensure_span_locked(source=event_type)
            if event_type == "assistant_delta":
                self._state.text_parts.append(content)
            else:
                self._state.progress_parts.append(content)
            self._state.buffered_chunk_count += 1
            if self._state.buffered_chunk_count == 1:
                add_event(
                    self._state.span,
                    "my_agent.channel.first_chunk_buffered",
                    {
                        "my_agent.channel.chunk_type": event_type,
                        "my_agent.channel.chunk_chars": len(content),
                    },
                )
        self._wake.set()
        return True

    def complete(self, final_text: str, timeout: float = 15.0) -> DeliveryReceipt:
        """Queue the final snapshot without blocking the Agent Turn on Feishu.

        The dedicated flush worker owns the eventual delivered/failed result
        and records it on ``send_response <channel>``.  ``timeout`` remains in the
        signature for compatibility with existing/custom stream handles.
        """
        del timeout
        with self._state.lock:
            if self._state.span is None:
                self._ensure_span_locked(source="final_response")
            self._state.final_text = final_text or ""
            self._state.done = True
            add_event(
                self._state.span,
                "my_agent.channel.final_chunk_buffered",
                {"my_agent.channel.final_chars": len(self._state.final_text)},
            )
            already_completed = self._state.completed.is_set()
            receipt = self._state.receipt
            provider_message_id = self._state.provider_message_id
        self._wake.set()
        if already_completed:
            return receipt
        return DeliveryReceipt.queued(provider_message_id)

    def _ensure_span_locked(self, *, source: str) -> None:
        if self._state.span is not None:
            return
        self._state.span = get_tracer().start_span(
            f"send_response {self._state.channel_type}",
            context=self._state.parent_context,
        )
        set_attributes(
            self._state.span,
            **{
                "messaging.system": self._state.channel_type,
                "my_agent.channel.first_output_source": source,
            },
        )

    def wait(self, timeout: float = 15.0) -> DeliveryReceipt:
        """Wait for the worker result when a caller explicitly needs it."""
        if not self._state.completed.wait(timeout=max(0.1, timeout)):
            return DeliveryReceipt.failed("Feishu final stream flush timed out", retryable=True)
        return self._state.receipt


class StreamFlushWorker:
    """One daemon worker batches all active Turn streams without blocking model SSE reads."""

    def __init__(self, interval: float = 0.75, char_threshold: int = 120):
        self.interval = max(0.1, float(interval))
        self.char_threshold = max(1, int(char_threshold))
        self._states: dict[str, _StreamState] = {}
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="feishu-flush-worker")
        self._thread.start()

    def open(self, callback: FlushCallback, *, channel_type: str = "feishu") -> StreamHandle:
        state = _StreamState(
            callback=callback,
            parent_context=current_context(),
            channel_type=str(channel_type or "unknown"),
            interval=self.interval,
            char_threshold=self.char_threshold,
        )
        with self._lock:
            self._states[state.id] = state
        self._wake.set()
        return StreamHandle(state, self._wake)

    def stop(self) -> None:
        self._running = False
        self._wake.set()
        self._thread.join(timeout=2.0)
        with self._lock:
            states = list(self._states.values())
            self._states.clear()
        for state in states:
            receipt = DeliveryReceipt.failed("Feishu stream worker stopped", retryable=True)
            with state.lock:
                state.receipt = receipt
                state.done = True
            if state.span is not None:
                set_error(state.span, receipt.error)
                state.span.end()
            state.completed.set()

    def _run(self) -> None:
        while self._running:
            self._wake.wait(timeout=0.1)
            self._wake.clear()
            with self._lock:
                states = list(self._states.values())
            now = time.monotonic()
            for state in states:
                self._flush_if_due(state, now)

    def _flush_if_due(self, state: _StreamState, now: float) -> None:
        with state.lock:
            text = state.visible_text()
            done = state.done
            changed_chars = max(0, len(text) - len(state.last_flushed_text))
            due = done or changed_chars >= state.char_threshold or (
                bool(text) and text != state.last_flushed_text and now - state.last_flush_at >= state.interval
            )
            if not due:
                return
            if text == state.last_flushed_text and not done:
                return
            provider_message_id = state.provider_message_id

        token = attach_context(state.parent_context)
        try:
            if state.span is None:
                # Defensive fallback for custom callers that bypass StreamHandle.
                state.span = get_tracer().start_span(
                    f"send_response {state.channel_type}",
                    context=state.parent_context,
                )
                set_attributes(state.span, **{"messaging.system": state.channel_type})
            started = time.monotonic()
            receipt = state.callback(text, provider_message_id, done)
            duration_ms = round((time.monotonic() - started) * 1000, 3)
            state.flush_count += 1
            add_event(
                state.span,
                "my_agent.channel.flush",
                {
                    "batch": state.flush_count,
                    # `text` is the cumulative user-visible snapshot. It is
                    # intentionally not a raw provider SSE chunk.
                    "chars": len(text),
                    "my_agent.channel.batch_source": "stream_buffer",
                    "my_agent.channel.snapshot_kind": "cumulative",
                    "my_agent.channel.total_chars": len(text),
                    "my_agent.channel.new_chars": changed_chars,
                    "duration_ms": duration_ms,
                    "final": done,
                    "status": receipt.status,
                    "delivered": receipt.delivered,
                    "retryable": receipt.retryable,
                    "error.message": receipt.error,
                },
            )
            with state.lock:
                state.receipt = receipt
                state.last_flush_at = now
                if receipt.delivered:
                    state.last_flushed_text = text
                    if receipt.provider_message_id:
                        state.provider_message_id = receipt.provider_message_id
            if done:
                add_content_event(state.span, "my_agent.feishu.final_content", text)
                set_attributes(
                    state.span,
                    **{
                        "my_agent.channel.flush_count": state.flush_count,
                        "my_agent.channel.final_chars": len(text),
                        "my_agent.channel.delivered": receipt.delivered,
                        "my_agent.channel.buffered_chunk_count": state.buffered_chunk_count,
                        "messaging.message.id": receipt.provider_message_id,
                    },
                )
                if not receipt.delivered:
                    set_error(state.span, receipt.error or "final Feishu flush failed")
                state.span.end()
                state.completed.set()
                with self._lock:
                    self._states.pop(state.id, None)
        except Exception as exc:
            receipt = DeliveryReceipt.failed(f"{type(exc).__name__}: {exc}", retryable=True)
            with state.lock:
                state.receipt = receipt
            if state.span is not None:
                set_error(state.span, receipt.error)
                if done:
                    state.span.end()
            if done:
                state.completed.set()
                with self._lock:
                    self._states.pop(state.id, None)
        finally:
            detach_context(token)
