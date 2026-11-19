from agent.run_context import TurnResult
from channel.cli.cli_channel import CONTINUE, PRODUCED, STOP, CLIChannel
from channel.conversation import CONVERSATION_ID_PATTERN
from channel.message import IncomingMessage
from channel.reply import Reply


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


def test_trace_command_is_no_longer_a_local_observability_backdoor(settings_factory, tmp_path):
    channel = _channel(settings_factory, tmp_path)
    assert channel._dispatch("/trace") == PRODUCED


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


class SummarizeAgent:
    def __init__(self, result="已压缩"):
        self.result = result
        self.calls = []
        self.run_called = False

    def summarize_session(self, session_id):
        self.calls.append(session_id)
        return self.result

    def run(self, *args, **kwargs):
        self.run_called = True
        raise AssertionError("summarize must not enter Agent.run")


def test_dispatch_summarize_does_not_produce(settings_factory, tmp_path, capsys):
    agent = SummarizeAgent("已压缩")
    channel = _channel(settings_factory, tmp_path, agent=agent)

    assert channel._dispatch("/summarize") == CONTINUE
    assert len(agent.calls) == 1
    assert agent.calls[0].startswith("cli:local:")
    assert CONVERSATION_ID_PATTERN.match(agent.calls[0].split(":")[-1])
    assert agent.run_called is False
    assert channel.sessions == {}
    assert "已压缩" in capsys.readouterr().out


def test_dispatch_summarize_noop_message(settings_factory, tmp_path, capsys):
    agent = SummarizeAgent("无需压缩")
    channel = _channel(settings_factory, tmp_path, agent=agent)
    channel._dispatch("/summarize")
    assert "无需压缩" in capsys.readouterr().out


def test_dispatch_normal_input_produces(settings_factory, tmp_path):
    channel = _channel(settings_factory, tmp_path)
    assert channel._dispatch("hello there") == PRODUCED
    keys = list(channel.sessions.keys())
    assert len(keys) == 1
    assert keys[0].startswith("cli:local:")
    assert CONVERSATION_ID_PATTERN.match(keys[0].split(":")[-1])
    queued = channel.sessions[keys[0]][0][0]
    assert isinstance(queued, IncomingMessage)
    assert queued.content == "hello there"
    assert queued.channel_type == "cli"
    assert queued.conversation_id == keys[0].split(":")[-1]


class ApprovalAgent:
    def __init__(self, result="已批准 [abcdef12]"):
        self.result = result
        self.calls = []
        self.run_called = False

    def approve_tool(self, approval_id, user_id, scope="once"):
        self.calls.append(("approve", approval_id, user_id, scope))
        return self.result

    def deny_tool(self, approval_id, user_id):
        self.calls.append(("deny", approval_id, user_id))
        return f"已拒绝 [{approval_id}]"

    def run(self, *args, **kwargs):
        self.run_called = True
        raise AssertionError("approval slash must not enter Agent.run")


def test_dispatch_approve_does_not_produce(settings_factory, tmp_path, capsys):
    agent = ApprovalAgent()
    channel = _channel(settings_factory, tmp_path, agent=agent)

    assert channel._dispatch("/approve abcdef12") == CONTINUE
    assert agent.calls == [("approve", "abcdef12", "local", "once")]
    assert agent.run_called is False
    assert channel.sessions == {}
    assert "已批准 [abcdef12]" in capsys.readouterr().out


def test_dispatch_approve_session_and_deny(settings_factory, tmp_path, capsys):
    agent = ApprovalAgent()
    channel = _channel(settings_factory, tmp_path, agent=agent)

    assert channel._dispatch("/approve abcdef12 session") == CONTINUE
    assert channel._dispatch("/deny abcdef12") == CONTINUE
    assert agent.calls == [
        ("approve", "abcdef12", "local", "session"),
        ("deny", "abcdef12", "local"),
    ]
    assert agent.run_called is False
    assert channel.sessions == {}
    out = capsys.readouterr().out
    assert "已批准" in out
    assert "已拒绝" in out


def test_dispatch_approve_without_id_prints_usage(settings_factory, tmp_path, capsys):
    agent = ApprovalAgent()
    channel = _channel(settings_factory, tmp_path, agent=agent)

    assert channel._dispatch("/approve") == CONTINUE
    assert agent.calls == []
    assert agent.run_called is False
    assert channel.sessions == {}
    assert "用法" in capsys.readouterr().out


def test_send_prints_reply(settings_factory, tmp_path, capsys):
    channel = _channel(settings_factory, tmp_path)
    msg = IncomingMessage(channel_type="cli", user_id="local", content="q", msg_id="1")
    channel.send(Reply("the answer"), msg)
    assert "the answer" in capsys.readouterr().out


def test_cli_streams_deltas_without_duplicating_final_answer(settings_factory, tmp_path, capsys):
    class StreamingAgent:
        def run(self, content, context=None, on_event=None):
            on_event({"type": "assistant_delta", "content": "第一块"})
            on_event({"type": "assistant_progress", "content": "正在调用工具"})
            on_event({"type": "assistant_delta", "content": "第二块"})
            return "第二块", TurnResult()

    channel = _channel(settings_factory, tmp_path, agent=StreamingAgent())
    msg = IncomingMessage(channel_type="cli", user_id="local", content="q", msg_id="stream-1")

    channel._handle(msg)

    out = capsys.readouterr().out
    assert "第一块" in out
    assert "第二块" in out
    assert out.count("第一块") == 1
    assert out.count("第二块") == 1
    assert "正在调用工具" not in out


def test_cli_stream_falls_back_to_completed_text_without_deltas(settings_factory, tmp_path, capsys):
    class CompletedOnlyAgent:
        def run(self, content, context=None, on_event=None):
            return "完整最终答案", TurnResult()

    channel = _channel(settings_factory, tmp_path, agent=CompletedOnlyAgent())
    msg = IncomingMessage(channel_type="cli", user_id="local", content="q", msg_id="stream-2")

    channel._handle(msg)

    assert capsys.readouterr().out.count("完整最终答案") == 1
