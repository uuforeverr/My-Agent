import threading
import time

from channel.delivery import DeliveryReceipt
from channel.streaming import StreamFlushWorker
from observability.otel import start_span


def test_flush_worker_batches_visible_text_and_preserves_parent_context(otel_exporter):
    calls = []

    def flush(text, message_id, final):
        calls.append((text, message_id, final, threading.current_thread().name))
        return DeliveryReceipt.success(message_id or "feishu-message-1")

    worker = StreamFlushWorker(interval=0.1, char_threshold=3)
    try:
        with start_span("turn my-agent", root=True) as root:
            handle = worker.open(flush)
            assert handle.publish({"type": "tool_call_delta", "content": "secret args"}) is False
            assert handle.publish({"type": "assistant_delta", "content": "abc"}) is True
            deadline = time.monotonic() + 2
            while not calls and time.monotonic() < deadline:
                time.sleep(0.01)
            queued = handle.complete("abc final")
            receipt = handle.wait(timeout=2)

        assert queued.status == "queued"
        assert queued.delivered is False
        assert bool(queued) is True
        assert receipt.delivered is True
        assert calls[0][:3] == ("abc", "", False)
        assert calls[-1][:3] == ("abc final", "feishu-message-1", True)
        assert all(call[3] == "feishu-flush-worker" for call in calls)
        stream = next(span for span in otel_exporter.get_finished_spans() if span.name == "send_response feishu")
        assert stream.parent.span_id == root.get_span_context().span_id
        root_span = next(span for span in otel_exporter.get_finished_spans() if span.name == "turn my-agent")
        assert root_span.end_time >= stream.end_time
        assert stream.attributes["my_agent.channel.delivered"] is True
        flush_events = [event for event in stream.events if event.name == "my_agent.channel.flush"]
        assert any(event.name == "my_agent.channel.first_chunk_buffered" for event in stream.events)
        assert any(event.name == "my_agent.channel.final_chunk_buffered" for event in stream.events)
        assert flush_events[0].attributes["my_agent.channel.batch_source"] == "stream_buffer"
        assert flush_events[0].attributes["my_agent.channel.snapshot_kind"] == "cumulative"
        assert flush_events[0].attributes["my_agent.channel.total_chars"] == 3
        assert flush_events[0].attributes["my_agent.channel.new_chars"] == 3
    finally:
        worker.stop()


def test_final_flush_failure_is_returned_without_throwing():
    worker = StreamFlushWorker(interval=0.1, char_threshold=10)
    try:
        handle = worker.open(
            lambda text, message_id, final: DeliveryReceipt.failed("feishu unavailable", retryable=True)
        )
        queued = handle.complete("answer")
        receipt = handle.wait(timeout=2)
        assert queued.status == "queued"
        assert receipt.delivered is False
        assert receipt.retryable is True
        assert "unavailable" in receipt.error
    finally:
        worker.stop()


def test_stop_releases_unfinished_stream_waiters():
    worker = StreamFlushWorker(interval=10, char_threshold=1000)
    handle = worker.open(lambda text, message_id, final: DeliveryReceipt.success("m1"))
    worker.stop()

    receipt = handle.complete("late", timeout=0.2)
    assert receipt.delivered is False
    assert "stopped" in receipt.error
