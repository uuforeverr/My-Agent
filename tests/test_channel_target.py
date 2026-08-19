from pathlib import Path

from agent.run_context import RunContext
from channel.message import IncomingMessage
from channel.target import ChannelTarget


def test_to_incoming_message_parses_conversation_id():
    target = ChannelTarget(
        channel_type="feishu",
        user_id="ou_xxx",
        session_id="feishu:ou_xxx:20260706-153012-a3f9",
    )
    msg = target.to_incoming_message()
    assert msg.conversation_id == "20260706-153012-a3f9"
    assert msg.session_id == "feishu:ou_xxx:20260706-153012-a3f9"


def test_to_incoming_message_without_conversation_keeps_legacy_session():
    target = ChannelTarget(
        channel_type="feishu",
        user_id="ou_xxx",
        session_id="feishu:ou_xxx",
    )
    msg = target.to_incoming_message()
    assert msg.conversation_id is None
    assert msg.session_id == "feishu:ou_xxx"


def test_to_incoming_message_carries_routing_fields():
    target = ChannelTarget(
        channel_type="feishu",
        user_id="ou_xxx",
        is_group=True,
        group_id="oc_yyy",
        session_id="feishu:oc_yyy",
    )
    msg = target.to_incoming_message()
    assert isinstance(msg, IncomingMessage)
    assert msg.channel_type == "feishu"
    assert msg.user_id == "ou_xxx"
    assert msg.is_group is True
    assert msg.group_id == "oc_yyy"
    assert msg.content == ""
    assert msg.msg_id.startswith("scheduler:")


def test_to_dict_and_from_dict_roundtrip():
    target = ChannelTarget("cli", "local", session_id="cli:local")
    restored = ChannelTarget.from_dict(target.to_dict())
    assert restored == target


def test_from_context_reads_metadata():
    ctx = RunContext.create(
        session_id="feishu:oc_yyy",
        workspace=Path("."),
        channel_type="feishu",
        user_id="ou_xxx",
        is_group=True,
        group_id="oc_yyy",
    )
    target = ChannelTarget.from_context(ctx)
    assert target is not None
    assert target.channel_type == "feishu"
    assert target.user_id == "ou_xxx"
    assert target.is_group is True
    assert target.group_id == "oc_yyy"
    assert target.session_id == "feishu:oc_yyy"


def test_from_context_returns_none_without_routing():
    ctx = RunContext.create(session_id="default", workspace=Path("."))
    assert ChannelTarget.from_context(ctx) is None
