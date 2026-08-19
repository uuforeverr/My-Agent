from channel.cli.cli_channel import CONTINUE, PRODUCED, CLIChannel
from channel.conversation import CONVERSATION_ID_PATTERN, new_conversation_id
from channel.feishu.feishu_channel import FeishuChannel
from channel.message import IncomingMessage
from tests.test_channel_feishu import _channel as _feishu_channel
from tests.test_channel_feishu import _text_event


def _assert_cid(cid: str) -> None:
    assert CONVERSATION_ID_PATTERN.match(cid), cid


def test_new_conversation_id_format_and_unique():
    first = new_conversation_id()
    second = new_conversation_id()
    _assert_cid(first)
    _assert_cid(second)
    assert first != second


def test_session_id_without_conversation_id_keeps_legacy():
    msg = IncomingMessage(channel_type="feishu", user_id="ou_abc", content="hi", msg_id="m1")
    assert msg.conversation_id is None
    assert msg.scope == "ou_abc"
    assert msg.session_id == "feishu:ou_abc"


def test_session_id_with_conversation_id_appends():
    msg = IncomingMessage(
        channel_type="feishu",
        user_id="ou_abc",
        content="hi",
        msg_id="m1",
        conversation_id="20260706-153012-a3f9",
    )
    assert msg.session_id == "feishu:ou_abc:20260706-153012-a3f9"


def test_current_conversation_is_stable_then_new_changes(settings_factory, tmp_path):
    settings = settings_factory(workspace=tmp_path / "w")
    channel = CLIChannel(agent=object(), settings=settings)

    first = channel.current_conversation("ou_abc")
    second = channel.current_conversation("ou_abc")
    _assert_cid(first)
    assert first == second

    changed = channel.start_new_conversation("ou_abc")
    _assert_cid(changed)
    assert changed != first
    assert channel.current_conversation("ou_abc") == changed

    other = channel.current_conversation("ou_other")
    assert other != changed


def test_conversation_id_survives_channel_reload(settings_factory, tmp_path):
    settings = settings_factory(workspace=tmp_path / "w")
    first = CLIChannel(agent=object(), settings=settings)
    cid = first.current_conversation("local")
    reloaded = CLIChannel(agent=object(), settings=settings)
    assert reloaded.current_conversation("local") == cid


def test_feishu_new_does_not_produce(settings_factory, tmp_path):
    channel = _feishu_channel(settings_factory, tmp_path)
    sent = []

    class GuardAgent:
        def run(self, *args, **kwargs):
            raise AssertionError("/new must not enter Agent.run")

    channel.agent = GuardAgent()
    channel.send = lambda reply, message: sent.append((reply.content, message.user_id))
    channel._handle_message_event(_text_event(text="/new", open_id="ou_abc"))

    assert channel.sessions == {}
    assert sent == [("🌱 已开启新对话", "ou_abc")]
    first = channel.current_conversation("ou_abc")
    channel._handle_message_event(_text_event(text="/new", msg_id="m2", open_id="ou_abc"))
    assert channel.current_conversation("ou_abc") != first


def test_feishu_normal_message_uses_current_conversation(settings_factory, tmp_path):
    channel = _feishu_channel(settings_factory, tmp_path)
    cid = channel.current_conversation("ou_abc")
    channel._handle_message_event(_text_event(text="你好", open_id="ou_abc"))

    session_id = f"feishu:ou_abc:{cid}"
    assert list(channel.sessions.keys()) == [session_id]
    msg = channel.sessions[session_id][0][0]
    assert msg.conversation_id == cid
    assert msg.session_id == session_id

    new_cid = channel.start_new_conversation("ou_abc")
    channel._handle_message_event(_text_event(text="下一句", msg_id="m2", open_id="ou_abc"))
    next_session = f"feishu:ou_abc:{new_cid}"
    assert next_session in channel.sessions
    follow = channel.sessions[next_session][0][0]
    assert follow.conversation_id == new_cid
    assert follow.content == "下一句"


def test_cli_new_then_next_line_uses_new_id(settings_factory, tmp_path, capsys):
    channel = CLIChannel(agent=object(), settings=settings_factory(workspace=tmp_path / "w"))
    first = channel.current_conversation("local")

    assert channel._dispatch("/new") == CONTINUE
    assert channel.sessions == {}
    out = capsys.readouterr().out
    assert "new conversation:" in out
    new_cid = channel.current_conversation("local")
    assert new_cid != first
    assert new_cid in out

    assert channel._dispatch("hello after new") == PRODUCED
    session_id = f"cli:local:{new_cid}"
    assert list(channel.sessions.keys()) == [session_id]
    queued = channel.sessions[session_id][0][0]
    assert queued.conversation_id == new_cid
    assert queued.content == "hello after new"


def test_two_conversations_have_isolated_history(settings_factory, tmp_path):
    from agent.agent import Agent
    from agent.run_context import RunContext
    from tests.conftest import FakeLLM

    root = tmp_path
    (root / "prompts").mkdir(parents=True, exist_ok=True)
    (root / "prompts" / "identity.md").write_text("You are test agent.", encoding="utf-8")
    (root / "skills").mkdir(parents=True, exist_ok=True)
    settings = settings_factory(project_root=root, workspace=root / "workspace")
    agent = Agent(settings)
    agent.llm = FakeLLM("ok")

    sid_a = "cli:local:20260706-153012-aaa1"
    sid_b = "cli:local:20260706-153013-bbb2"
    agent.run("alpha-only", context=RunContext.create(sid_a, settings.workspace, entrypoint="cli"))
    agent.run("beta-only", context=RunContext.create(sid_b, settings.workspace, entrypoint="cli"))

    history_a = [m.get("content") for m in agent._get_context_manager(sid_a).history]
    history_b = [m.get("content") for m in agent._get_context_manager(sid_b).history]
    assert "alpha-only" in history_a
    assert "beta-only" not in history_a
    assert "beta-only" in history_b
    assert "alpha-only" not in history_b
    agent.memory.close()
