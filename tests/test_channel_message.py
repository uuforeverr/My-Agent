from channel.message import IncomingMessage
from channel.reply import Reply


def test_p2p_session_id_uses_user_id():
    msg = IncomingMessage(channel_type="feishu", user_id="ou_abc", content="hi", msg_id="m1")
    assert msg.session_id == "feishu:ou_abc"
    assert msg.is_group is False
    assert msg.raw == {}


def test_group_session_id_uses_group_id():
    msg = IncomingMessage(
        channel_type="feishu",
        user_id="ou_abc",
        content="hi",
        msg_id="m1",
        is_group=True,
        group_id="oc_group",
    )
    assert msg.session_id == "feishu:oc_group"


def test_group_without_group_id_falls_back_to_user():
    msg = IncomingMessage(
        channel_type="feishu",
        user_id="ou_abc",
        content="hi",
        msg_id="m1",
        is_group=True,
    )
    assert msg.session_id == "feishu:ou_abc"


def test_reply_defaults_to_text():
    reply = Reply("hello")
    assert reply.content == "hello"
    assert reply.type == "text"
