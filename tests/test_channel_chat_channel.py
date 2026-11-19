import threading
import time

from agent.run_context import TurnResult
from channel.chat_channel import ChatChannel
from channel.message import IncomingMessage
from channel.reply import Reply

WAIT_TIMEOUT = 2.0


def _msg(session_user, content="hi", channel_type="cli"):
    return IncomingMessage(
        channel_type=channel_type,
        user_id=session_user,
        content=content,
        msg_id=f"{session_user}-{content}",
    )


class RecordingChannel(ChatChannel):
    channel_type = "cli"

    def __init__(self, agent, settings, **kwargs):
        super().__init__(agent, settings, **kwargs)
        self.sent: list = []
        self.sent_event = threading.Event()

    def send(self, reply: Reply, message: IncomingMessage) -> None:
        self.sent.append((message.session_id, reply.content))
        self.sent_event.set()


class GateAgent:
    """Agent stub whose run() blocks on a per-call gate event.

    Records the order handlers enter run(); each run waits until its gate is
    released, enabling deterministic serial-vs-parallel assertions.
    """

    def __init__(self):
        self.entered = []
        self.entered_lock = threading.Lock()
        self.gate = threading.Event()
        self.enter_signal = threading.Event()

    def run(self, content, context=None, on_event=None):
        with self.entered_lock:
            self.entered.append(content)
        self.enter_signal.set()
        self.gate.wait(timeout=WAIT_TIMEOUT)
        return f"reply:{content}", None


class BarrierAgent:
    """Agent stub whose run() rendezvous at a Barrier(2).

    If two handlers cannot be in run() simultaneously, barrier.wait times out
    and raises BrokenBarrierError -> the parallelism assertion fails.
    """

    def __init__(self):
        self.barrier = threading.Barrier(2)
        self.met = threading.Event()

    def run(self, content, context=None, on_event=None):
        self.barrier.wait(timeout=WAIT_TIMEOUT)
        self.met.set()
        return f"reply:{content}", None


def test_produce_routes_into_per_session_queues(settings_factory, tmp_path):
    settings = settings_factory(workspace=tmp_path / "w")
    channel = RecordingChannel(agent=object(), settings=settings)

    channel.produce(_msg("a"))
    channel.produce(_msg("a", content="hi2"))
    channel.produce(_msg("b"))

    assert set(channel.sessions.keys()) == {"cli:a", "cli:b"}
    assert len(channel.sessions["cli:a"][0]) == 2
    assert len(channel.sessions["cli:b"][0]) == 1


def test_same_session_runs_serially(settings_factory, tmp_path):
    settings = settings_factory(workspace=tmp_path / "w")
    agent = GateAgent()
    channel = RecordingChannel(agent=agent, settings=settings, poll_interval=0.01)
    channel.start_consuming()

    channel.produce(_msg("a", content="first"))
    channel.produce(_msg("a", content="second"))

    # First handler enters run() and blocks on the gate.
    assert agent.enter_signal.wait(timeout=WAIT_TIMEOUT)
    # While the first is blocked, the second must NOT have entered (serial).
    with agent.entered_lock:
        assert agent.entered == ["first"]

    # Release the gate; both handlers now complete.
    agent.gate.set()
    deadline = threading.Event()

    def _both_done():
        return len(agent.entered) == 2

    # Poll until the second handler ran (bounded by timeout).
    waited = 0.0
    while not _both_done() and waited < WAIT_TIMEOUT:
        deadline.wait(0.02)
        waited += 0.02
    assert agent.entered == ["first", "second"]
    channel.stop()


def test_different_sessions_run_in_parallel(settings_factory, tmp_path):
    settings = settings_factory(workspace=tmp_path / "w")
    agent = BarrierAgent()
    channel = RecordingChannel(agent=agent, settings=settings, poll_interval=0.01)
    channel.start_consuming()

    channel.produce(_msg("a", content="ta"))
    channel.produce(_msg("b", content="tb"))

    # Both handlers must reach the barrier at the same time; otherwise the
    # barrier times out and met stays unset.
    assert agent.met.wait(timeout=WAIT_TIMEOUT)
    channel.stop()


def test_handle_builds_run_context_and_sends(settings_factory, tmp_path):
    settings = settings_factory(workspace=tmp_path / "w")

    class CtxAgent:
        def __init__(self):
            self.seen_context = None

        def run(self, content, context=None, on_event=None):
            self.seen_context = context
            return f"answer:{content}", TurnResult()

    agent = CtxAgent()
    channel = RecordingChannel(agent=agent, settings=settings)

    channel._handle(_msg("a", content="hello", channel_type="cli"))

    ctx = agent.seen_context
    assert ctx.session_id == "cli:a"
    assert ctx.get("channel_type") == "cli"
    assert ctx.get("entrypoint") == "cli"
    assert ctx.get("user_id") == "a"
    assert channel.sent == [("cli:a", "answer:hello")]


def test_handle_sends_assistant_progress_before_final(settings_factory, tmp_path):
    settings = settings_factory(workspace=tmp_path / "w")

    class ProgressAgent:
        def run(self, content, context=None, on_event=None):
            on_event({
                "type": "assistant_progress",
                "content": "让我翻翻关于你的记忆～",
            })
            return "最终答案", TurnResult()

    channel = RecordingChannel(agent=ProgressAgent(), settings=settings)

    channel._handle(_msg("a", content="who am i", channel_type="cli"))

    assert channel.sent == [
        ("cli:a", "让我翻翻关于你的记忆～"),
        ("cli:a", "最终答案"),
    ]


def test_handle_sends_approval_request_before_final(settings_factory, tmp_path):
    settings = settings_factory(workspace=tmp_path / "w")

    class ApprovalProgressAgent:
        def run(self, content, context=None, on_event=None):
            on_event({
                "type": "approval_request",
                "content": "待审批 [abcdef12]\n工具: bash    等级: L2",
                "approval_id": "abcdef12",
            })
            return "已执行", TurnResult()

    channel = RecordingChannel(agent=ApprovalProgressAgent(), settings=settings)
    channel._handle(_msg("a", content="run tests", channel_type="cli"))

    assert channel.sent == [
        ("cli:a", "待审批 [abcdef12]\n工具: bash    等级: L2"),
        ("cli:a", "已执行"),
    ]


def test_handle_exports_success_turn_to_native_otel(settings_factory, tmp_path, otel_exporter):
    settings = settings_factory(workspace=tmp_path / "w")

    class TraceAgent:
        def run(self, content, context=None, on_event=None):
            return "ok", TurnResult(outcome="success")

    channel = RecordingChannel(agent=TraceAgent(), settings=settings)

    channel._handle(_msg("a", content="hello", channel_type="cli"))

    spans = otel_exporter.get_finished_spans()
    root = next(span for span in spans if span.name == "turn my-agent")
    ingress = next(span for span in spans if span.name == "receive_request cli")
    response = next(span for span in spans if span.name == "send_response cli")
    assert ingress.parent.span_id == root.context.span_id
    assert response.parent.span_id == root.context.span_id
    assert root.attributes["my_agent.run.outcome"] == "success"
    assert root.end_time >= response.end_time


def test_handle_error_is_reported_and_semaphore_released(settings_factory, tmp_path, otel_exporter):
    settings = settings_factory(workspace=tmp_path / "w")

    class BoomAgent:
        def run(self, content, context=None, on_event=None):
            raise RuntimeError("boom")

    channel = RecordingChannel(agent=BoomAgent(), settings=settings, poll_interval=0.01)
    channel.start_consuming()
    channel.produce(_msg("a", content="x"))

    assert channel.sent_event.wait(timeout=WAIT_TIMEOUT)
    assert channel.sent[0][0] == "cli:a"
    assert "boom" in channel.sent[0][1]
    deadline = time.monotonic() + WAIT_TIMEOUT
    roots = []
    while not roots and time.monotonic() < deadline:
        roots = [span for span in otel_exporter.get_finished_spans() if span.name == "turn my-agent"]
        time.sleep(0.01)
    assert roots[0].status.status_code.name == "ERROR"
    assert roots[0].attributes["my_agent.run.outcome"] == "failed"

    # The semaphore for the session must have been released, so a follow-up
    # message on the same session is still processed (no deadlock).
    channel.sent_event.clear()
    channel.produce(_msg("a", content="y"))
    assert channel.sent_event.wait(timeout=WAIT_TIMEOUT)
    channel.stop()
