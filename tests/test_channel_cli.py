from channel.cli.cli_channel import CONTINUE, PRODUCED, STOP, CLIChannel
from channel.message import IncomingMessage
from channel.reply import Reply
from observability import Trace


class DreamAgent:
    def __init__(self, result):
        self.memory = self
        self._result = result
        self.dreamed = False

    def deep_dream(self, lookback_days=1):
        self.dreamed = True
        return self._result


def _channel(settings_factory, tmp_path, agent=None):
    settings = settings_factory(workspace=tmp_path / "w")
    return CLIChannel(agent=agent or object(), settings=settings)


def test_dispatch_exit_and_quit_stop(settings_factory, tmp_path):
    channel = _channel(settings_factory, tmp_path)
    assert channel._dispatch("exit") == STOP
    assert channel._dispatch("QUIT") == STOP


def test_dispatch_blank_continues(settings_factory, tmp_path):
    channel = _channel(settings_factory, tmp_path)
    assert channel._dispatch("") == CONTINUE
    assert channel.sessions == {}


def test_dispatch_trace_without_trace(settings_factory, tmp_path, capsys):
    channel = _channel(settings_factory, tmp_path)
    assert channel._dispatch("/trace") == CONTINUE
    assert "No trace yet." in capsys.readouterr().out


def test_dispatch_trace_prints_existing_trace(settings_factory, tmp_path, capsys):
    channel = _channel(settings_factory, tmp_path)

    class FakeTrace:
        def to_json(self, pretty=False):
            return '{"events": []}'

    channel.last_trace = FakeTrace()
    assert channel._dispatch("/trace") == CONTINUE
    assert '"events"' in capsys.readouterr().out


def test_dispatch_trace_by_id_prints_persisted_trace(settings_factory, tmp_path, capsys):
    channel = _channel(settings_factory, tmp_path)
    trace = Trace(request_id="persisted-1")
    with trace.start_span("agent.invoke", span_kind="agent.invoke", session_id="cli:local"):
        pass
    channel.trace_store.append(trace)

    assert channel._dispatch("/trace persisted-1") == CONTINUE

    out = capsys.readouterr().out
    assert "trace persisted-1" in out
    assert "span tree" in out
    assert "agent.invoke" in out
    assert '"trace_id": "persisted-1"' in out


def test_dispatch_trace_by_id_missing(settings_factory, tmp_path, capsys):
    channel = _channel(settings_factory, tmp_path)

    assert channel._dispatch("/trace missing") == CONTINUE

    assert "Trace not found: missing" in capsys.readouterr().out


def test_dispatch_traces_lists_persisted_traces(settings_factory, tmp_path, capsys):
    channel = _channel(settings_factory, tmp_path)
    success = Trace(request_id="success-1")
    with success.start_span("agent.invoke", span_kind="agent.invoke"):
        pass
    error = Trace(request_id="error-1")
    try:
        with error.start_span("tool.execute", span_kind="tool.execute"):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    channel.trace_store.append(success)
    channel.trace_store.append(error)

    assert channel._dispatch("/traces 1") == CONTINUE
    out = capsys.readouterr().out
    assert "error-1" in out
    assert "success-1" not in out

    assert channel._dispatch("/traces 10 status=success") == CONTINUE
    out = capsys.readouterr().out
    assert "success-1" in out
    assert "error-1" not in out


def test_dispatch_dream_invokes_memory(settings_factory, tmp_path, capsys):
    agent = DreamAgent({"ok": True, "dream_path": "/tmp/diary.md"})
    channel = _channel(settings_factory, tmp_path, agent=agent)
    assert channel._dispatch("/dream") == CONTINUE
    assert agent.dreamed is True
    assert "diary" in capsys.readouterr().out


def test_dispatch_dream_skipped(settings_factory, tmp_path, capsys):
    agent = DreamAgent({"ok": False, "reason": "no episodic"})
    channel = _channel(settings_factory, tmp_path, agent=agent)
    channel._dispatch("/dream")
    assert "skipped" in capsys.readouterr().out


def test_dispatch_normal_input_produces(settings_factory, tmp_path):
    channel = _channel(settings_factory, tmp_path)
    assert channel._dispatch("hello there") == PRODUCED
    assert list(channel.sessions.keys()) == ["cli:local"]
    queued = channel.sessions["cli:local"][0][0]
    assert isinstance(queued, IncomingMessage)
    assert queued.content == "hello there"
    assert queued.channel_type == "cli"


def test_send_prints_reply(settings_factory, tmp_path, capsys):
    channel = _channel(settings_factory, tmp_path)
    msg = IncomingMessage(channel_type="cli", user_id="local", content="q", msg_id="1")
    channel.send(Reply("the answer"), msg)
    assert "the answer" in capsys.readouterr().out


def test_parse_traces_args_supports_filters():
    query = CLIChannel._parse_traces_args(
        "/traces 20 status=error session_id=cli:local entrypoint=cli "
        "before=trace-1 start=2026-07-15T10:00:00 end=2026-07-15T11:00:00"
    )

    assert query.limit == 20
    assert query.status == "error"
    assert query.session_id == "cli:local"
    assert query.entrypoint == "cli"
    assert query.before == "trace-1"
    assert query.start == "2026-07-15T10:00:00"
    assert query.end == "2026-07-15T11:00:00"
