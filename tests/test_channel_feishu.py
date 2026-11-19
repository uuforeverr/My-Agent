import json
import sys
import time
import types
import urllib.error
from io import BytesIO

from channel.conversation import CONVERSATION_ID_PATTERN
from channel.feishu.feishu_channel import FeishuChannel
from channel.message import IncomingMessage
from channel.reply import Reply


def _only_session(channel, prefix: str) -> tuple[str, str]:
    keys = list(channel.sessions.keys())
    assert len(keys) == 1
    key = keys[0]
    assert key.startswith(prefix + ":"), key
    cid = key[len(prefix) + 1 :]
    assert CONVERSATION_ID_PATTERN.match(cid), cid
    return key, cid


def _channel(settings_factory, tmp_path):
    settings = settings_factory(
        workspace=tmp_path / "w",
        feishu_app_id="app-id",
        feishu_app_secret="app-secret",
    )
    return FeishuChannel(agent=object(), settings=settings)


def _text_event(text="hello", msg_id="m1", chat_type="p2p", open_id="ou_1",
                create_time=None, mentions=None, chat_id="oc_1"):
    if create_time is None:
        create_time = str(int(time.time() * 1000))
    message = {
        "message_id": msg_id,
        "create_time": create_time,
        "chat_type": chat_type,
        "chat_id": chat_id,
        "message_type": "text",
        "content": json.dumps({"text": text}),
    }
    if mentions is not None:
        message["mentions"] = mentions
    return {
        "message": message,
        "sender": {"sender_id": {"open_id": open_id}},
    }


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps(self._payload).encode("utf-8")


def test_handle_p2p_text_event_produces_message(settings_factory, tmp_path):
    channel = _channel(settings_factory, tmp_path)
    channel._handle_message_event(_text_event(text="你好", open_id="ou_abc"))

    session_id, cid = _only_session(channel, "feishu:ou_abc")
    msg = channel.sessions[session_id][0][0]
    assert isinstance(msg, IncomingMessage)
    assert msg.user_id == "ou_abc"
    assert msg.content == "你好"
    assert msg.conversation_id == cid
    assert msg.session_id == f"feishu:ou_abc:{cid}"


def test_summarize_does_not_produce_or_run_agent(settings_factory, tmp_path):
    channel = _channel(settings_factory, tmp_path)
    sent = []

    class SummarizeAgent:
        def summarize_session(self, session_id):
            self.session_id = session_id
            return "已压缩"

        def run(self, *args, **kwargs):
            raise AssertionError("summarize must not enter Agent.run")

    channel.agent = SummarizeAgent()
    channel.send = lambda reply, message: sent.append((reply.content, message.session_id))
    channel._handle_message_event(_text_event(text="/summarize", open_id="ou_abc"))

    assert channel.sessions == {}
    assert sent[0][0] == "已压缩"
    session_id = sent[0][1]
    assert session_id.startswith("feishu:ou_abc:")
    assert CONVERSATION_ID_PATTERN.match(session_id.split(":")[-1])
    assert channel.agent.session_id == session_id


def test_approve_does_not_produce_or_run_agent(settings_factory, tmp_path):
    channel = _channel(settings_factory, tmp_path)
    sent = []

    class ApprovalAgent:
        def __init__(self):
            self.calls = []

        def approve_tool(self, approval_id, user_id, scope="once"):
            self.calls.append(("approve", approval_id, user_id, scope))
            return f"已批准 [{approval_id}]"

        def deny_tool(self, approval_id, user_id):
            self.calls.append(("deny", approval_id, user_id))
            return f"已拒绝 [{approval_id}]"

        def run(self, *args, **kwargs):
            raise AssertionError("approval slash must not enter Agent.run")

    agent = ApprovalAgent()
    channel.agent = agent
    channel.send = lambda reply, message: sent.append((reply.content, message.user_id, message.session_id))
    channel._handle_message_event(_text_event(text="/approve abcdef12 session", open_id="ou_abc"))

    assert channel.sessions == {}
    assert agent.calls == [("approve", "abcdef12", "ou_abc", "session")]
    assert sent[0][0] == "已批准 [abcdef12]"
    assert sent[0][1] == "ou_abc"
    assert sent[0][2].startswith("feishu:ou_abc:")
    assert CONVERSATION_ID_PATTERN.match(sent[0][2].split(":")[-1])


def test_group_other_user_cannot_approve(settings_factory, tmp_path):
    from tools.approval import ApprovalGate
    from tools.policy import ToolRequest

    channel = _channel(settings_factory, tmp_path)
    sent = []
    gate = ApprovalGate(timeout_seconds=2)
    pending = gate.submit(
        ToolRequest(
            name="bash",
            arguments={"command": "python -c pass"},
            risk_level="L2",
            user_id="ou_owner",
            session_id="feishu:oc_group",
        )
    )

    class GateAgent:
        def approve_tool(self, approval_id, user_id, scope="once"):
            return gate.approve(approval_id, user_id, scope)[1]

        def run(self, *args, **kwargs):
            raise AssertionError("approval slash must not enter Agent.run")

    channel.agent = GateAgent()
    channel.send = lambda reply, message: sent.append((reply.content, message.user_id))
    channel._handle_message_event(_text_event(
        text=f"/approve {pending.approval_id}",
        msg_id="m-other",
        chat_type="group",
        open_id="ou_other",
        mentions=[{"key": "@_user_1"}],
        chat_id="oc_group",
    ))

    assert channel.sessions == {}
    assert sent == [("无权审批该操作", "ou_other")]
    assert gate.pending_ids() == [pending.approval_id]

    channel._handle_message_event(_text_event(
        text=f"/approve {pending.approval_id}",
        msg_id="m-owner",
        chat_type="group",
        open_id="ou_owner",
        mentions=[{"key": "@_user_1"}],
        chat_id="oc_group",
    ))
    assert sent[-1] == (f"已批准 [{pending.approval_id}]（仅本次）", "ou_owner")
    assert gate.pending_ids() == []


def test_duplicate_msg_id_ignored(settings_factory, tmp_path):
    channel = _channel(settings_factory, tmp_path)
    channel._handle_message_event(_text_event(msg_id="dup"))
    channel._handle_message_event(_text_event(msg_id="dup", text="second"))

    session_id, _cid = _only_session(channel, "feishu:ou_1")
    assert len(channel.sessions[session_id][0]) == 1


def test_group_without_mention_ignored(settings_factory, tmp_path):
    channel = _channel(settings_factory, tmp_path)
    channel._handle_message_event(_text_event(chat_type="group", mentions=None))
    assert channel.sessions == {}


def test_group_with_mention_uses_chat_id_session(settings_factory, tmp_path):
    channel = _channel(settings_factory, tmp_path)
    channel._handle_message_event(_text_event(
        chat_type="group",
        mentions=[{"key": "@_user_1"}],
        chat_id="oc_group",
    ))
    session_id, cid = _only_session(channel, "feishu:oc_group")
    msg = channel.sessions[session_id][0][0]
    assert msg.is_group is True
    assert msg.group_id == "oc_group"
    assert msg.conversation_id == cid


def test_group_mention_matches_bot_open_id(settings_factory, tmp_path):
    channel = _channel(settings_factory, tmp_path)
    channel._bot_open_id = "ou_bot"
    channel._handle_message_event(_text_event(
        chat_type="group",
        mentions=[{"id": {"open_id": "ou_bot"}}],
        chat_id="oc_group",
    ))
    _only_session(channel, "feishu:oc_group")


def test_group_mention_of_someone_else_ignored(settings_factory, tmp_path):
    channel = _channel(settings_factory, tmp_path)
    channel._bot_open_id = "ou_bot"
    # User @-mentioned another human, not the bot. With "read all group messages"
    # granted, Feishu still delivers this; we must NOT respond.
    channel._handle_message_event(_text_event(
        chat_type="group",
        mentions=[{"id": {"open_id": "ou_someone_else"}}],
        chat_id="oc_group",
    ))
    assert channel.sessions == {}


def test_group_mention_without_bot_open_id_falls_back(settings_factory, tmp_path):
    channel = _channel(settings_factory, tmp_path)
    channel._bot_open_id = None  # bot-info fetch failed
    channel._handle_message_event(_text_event(
        chat_type="group",
        mentions=[{"id": {"open_id": "ou_anyone"}}],
        chat_id="oc_group",
    ))
    # Fallback: any mention counts as the bot so we don't go silent.
    _only_session(channel, "feishu:oc_group")


def test_fetch_bot_open_id_parses_response(settings_factory, tmp_path, monkeypatch):
    channel = _channel(settings_factory, tmp_path)
    monkeypatch.setattr(channel, "fetch_access_token", lambda: "tok")
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout: _Resp({"code": 0, "bot": {"open_id": "ou_bot_x"}}),
    )
    channel._fetch_bot_open_id()
    assert channel._bot_open_id == "ou_bot_x"


def test_fetch_bot_open_id_no_token_skips(settings_factory, tmp_path, monkeypatch):
    channel = _channel(settings_factory, tmp_path)
    monkeypatch.setattr(channel, "fetch_access_token", lambda: "")
    channel._fetch_bot_open_id()
    assert channel._bot_open_id is None


def test_stale_message_filtered(settings_factory, tmp_path):
    channel = _channel(settings_factory, tmp_path)
    old = str(int((time.time() - 120) * 1000))
    channel._handle_message_event(_text_event(create_time=old))
    assert channel.sessions == {}


def test_invalid_event_ignored(settings_factory, tmp_path):
    channel = _channel(settings_factory, tmp_path)
    channel._handle_message_event({"message": {}, "sender": {}})
    channel._handle_message_event({})
    assert channel.sessions == {}


def test_non_text_message_ignored(settings_factory, tmp_path):
    channel = _channel(settings_factory, tmp_path)
    event = _text_event()
    event["message"]["message_type"] = "image"
    channel._handle_message_event(event)
    assert channel.sessions == {}


def test_empty_text_ignored(settings_factory, tmp_path):
    channel = _channel(settings_factory, tmp_path)
    channel._handle_message_event(_text_event(text="   "))
    assert channel.sessions == {}


def test_missing_open_id_ignored(settings_factory, tmp_path):
    channel = _channel(settings_factory, tmp_path)
    event = _text_event()
    event["sender"] = {"sender_id": {}}
    channel._handle_message_event(event)
    assert channel.sessions == {}


def test_send_without_token_short_circuits(settings_factory, tmp_path, monkeypatch):
    channel = _channel(settings_factory, tmp_path)
    monkeypatch.setattr(channel, "fetch_access_token", lambda: "")

    called = {"sent": False}

    def fake_urlopen(req, timeout):
        called["sent"] = True
        return _Resp({"code": 0})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    msg = IncomingMessage(channel_type="feishu", user_id="ou_x", content="q", msg_id="m1")
    channel.send(Reply("hi"), msg)
    assert called["sent"] is False


def test_send_builds_p2p_request(settings_factory, tmp_path, monkeypatch):
    channel = _channel(settings_factory, tmp_path)
    monkeypatch.setattr(channel, "fetch_access_token", lambda: "tok-123")

    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["headers"] = req.headers
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _Resp({"code": 0})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    msg = IncomingMessage(channel_type="feishu", user_id="ou_x", content="q", msg_id="m1")
    channel.send(Reply("hi there"), msg)

    assert "receive_id_type=open_id" in captured["url"]
    assert captured["headers"]["Authorization"] == "Bearer tok-123"
    assert captured["body"]["receive_id"] == "ou_x"
    assert captured["body"]["msg_type"] == "text"
    assert json.loads(captured["body"]["content"]) == {"text": "hi there"}


def test_send_group_uses_chat_id(settings_factory, tmp_path, monkeypatch):
    channel = _channel(settings_factory, tmp_path)
    monkeypatch.setattr(channel, "fetch_access_token", lambda: "tok")
    captured = {}

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _Resp({"code": 0})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    msg = IncomingMessage(
        channel_type="feishu", user_id="ou_x", content="q", msg_id="m1",
        is_group=True, group_id="oc_g",
    )
    channel.send(Reply("hey"), msg)
    assert "receive_id_type=chat_id" in captured["url"]
    assert captured["body"]["receive_id"] == "oc_g"


def test_approval_event_sends_card_2_with_callback_buttons(settings_factory, tmp_path, monkeypatch):
    channel = _channel(settings_factory, tmp_path)
    monkeypatch.setattr(channel, "fetch_access_token", lambda: "tok")
    captured = {}

    def fake_urlopen(req, timeout):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _Resp({"code": 0, "data": {"message_id": "om_approval"}})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    msg = IncomingMessage(channel_type="feishu", user_id="ou_x", content="q", msg_id="m1")
    event = {
        "type": "approval_request",
        "content": "待审批 [abcdef12]",
        "approval_id": "abcdef12",
        "tool_name": "scheduler",
        "risk_level": "L2",
        "argument_lines": "  action: create\n  password: [redacted]",
        "timeout_seconds": 120,
        "session_scope_allowed": True,
    }

    receipt = channel._send_event(event, msg)

    assert receipt.delivered is True
    assert captured["body"]["msg_type"] == "interactive"
    card = json.loads(captured["body"]["content"])
    assert card["schema"] == "2.0"
    assert card["header"]["template"] == "yellow"
    buttons = [
        column["elements"][0]
        for element in card["body"]["elements"]
        if element.get("tag") == "column_set"
        for column in element.get("columns", [])
        if column.get("elements") and column["elements"][0].get("tag") == "button"
    ]
    assert [button["text"]["content"] for button in buttons] == [
        "仅本次允许",
        "本会话允许",
        "拒绝",
    ]
    assert buttons[0]["behaviors"][0]["value"] == {
        "source": "my-agent-tool-approval",
        "approval_id": "abcdef12",
        "action": "approve",
        "scope": "once",
    }
    assert "[redacted]" in json.dumps(card, ensure_ascii=False)


def test_bash_approval_card_omits_session_wide_button(settings_factory, tmp_path):
    channel = _channel(settings_factory, tmp_path)
    event = channel._normalize_approval_event({
        "approval_id": "abcdef12",
        "tool_name": "bash",
        "risk_level": "L2",
        "argument_lines": "  command: pytest",
        "session_scope_allowed": False,
    })

    card = channel._build_approval_card(event)
    rendered = json.dumps(card, ensure_ascii=False)
    assert "仅本次允许" in rendered
    assert "本会话允许" not in rendered
    assert "拒绝" in rendered


def test_scheduled_approval_reply_is_upgraded_from_text_to_card(settings_factory, tmp_path, monkeypatch):
    channel = _channel(settings_factory, tmp_path)
    monkeypatch.setattr(channel, "fetch_access_token", lambda: "tok")
    captured = {}

    def fake_urlopen(req, timeout):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _Resp({"code": 0, "data": {"message_id": "om_scheduled"}})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    msg = IncomingMessage(channel_type="feishu", user_id="ou_x", content="", msg_id="scheduled")
    prompt = (
        "待审批 [abcdef12]\n"
        "工具: scheduler    等级: L2\n"
        "参数:\n"
        "  action: create\n\n"
        "/approve abcdef12          仅本次\n"
        "/approve abcdef12 session  本会话允许同类工具\n"
        "/deny abcdef12             拒绝\n"
        "超时 24h 将自动拒绝。"
    )

    receipt = channel.send(Reply(prompt, type="approval"), msg)

    assert receipt.delivered is True
    assert captured["body"]["msg_type"] == "interactive"
    card = json.loads(captured["body"]["content"])
    assert card["schema"] == "2.0"
    assert "24h" in json.dumps(card, ensure_ascii=False)


def test_approval_card_callback_resolves_gate_and_queues_card_update(settings_factory, tmp_path):
    channel = _channel(settings_factory, tmp_path)

    class ApprovalAgent:
        def __init__(self):
            self.calls = []

        def approve_tool(self, approval_id, user_id, scope="once"):
            self.calls.append((approval_id, user_id, scope))
            return f"已批准 [{approval_id}]（本会话允许同类工具）"

    agent = ApprovalAgent()
    channel.agent = agent
    channel._remember_approval_card("abcdef12", channel._normalize_approval_event({
        "approval_id": "abcdef12",
        "tool_name": "scheduler",
        "risk_level": "L2",
        "argument_lines": "  action: create",
    }))
    updates = []
    channel._queue_approval_card_update = lambda message_id, card: updates.append((message_id, card))

    response = channel._handle_card_action_event({
        "operator": {"open_id": "ou_owner"},
        "context": {"open_message_id": "om_approval"},
        "action": {
            "value": {
                "source": "my-agent-tool-approval",
                "approval_id": "abcdef12",
                "action": "approve",
                "scope": "session",
            }
        },
    })

    assert agent.calls == [("abcdef12", "ou_owner", "session")]
    assert response["toast"]["type"] == "success"
    assert updates[0][0] == "om_approval"
    assert updates[0][1]["header"]["template"] == "green"
    assert "behaviors" not in json.dumps(updates[0][1], ensure_ascii=False)
    assert channel._get_approval_card("abcdef12") is None


def test_approval_card_callback_keeps_card_on_authorization_failure(settings_factory, tmp_path):
    channel = _channel(settings_factory, tmp_path)

    class ApprovalAgent:
        def approve_tool(self, approval_id, user_id, scope="once"):
            return "无权审批该操作"

    channel.agent = ApprovalAgent()
    updates = []
    channel._queue_approval_card_update = lambda message_id, card: updates.append((message_id, card))
    response = channel._handle_card_action_event({
        "operator": {"open_id": "ou_other"},
        "context": {"open_message_id": "om_approval"},
        "action": {
            "value": {
                "source": "my-agent-tool-approval",
                "approval_id": "abcdef12",
                "action": "approve",
                "scope": "once",
            }
        },
    })

    assert response == {"toast": {"type": "error", "content": "无权审批该操作"}}
    assert updates == []


def test_update_interactive_approval_message(settings_factory, tmp_path, monkeypatch):
    channel = _channel(settings_factory, tmp_path)
    monkeypatch.setattr(channel, "fetch_access_token", lambda: "tok")
    captured = {}

    def fake_urlopen(req, timeout):
        captured["method"] = req.method
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _Resp({"code": 0})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    card = {"schema": "2.0", "body": {"elements": []}}

    receipt = channel._update_interactive_message("om_approval", card)

    assert receipt.delivered is True
    assert captured["method"] == "PUT"
    assert captured["url"].endswith("/im/v1/messages/om_approval")
    assert captured["body"]["msg_type"] == "interactive"
    assert json.loads(captured["body"]["content"]) == card


def test_stream_flush_updates_existing_feishu_message(settings_factory, tmp_path, monkeypatch):
    channel = _channel(settings_factory, tmp_path)
    monkeypatch.setattr(channel, "fetch_access_token", lambda: "tok")
    captured = {}

    def fake_urlopen(req, timeout):
        captured["method"] = req.method
        captured["url"] = req.full_url
        captured["headers"] = req.headers
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _Resp({"code": 0})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    msg = IncomingMessage(channel_type="feishu", user_id="ou_x", content="q", msg_id="m1")

    receipt = channel._flush_stream_message(msg, "partial answer", "om_message_1")

    assert receipt.delivered is True
    assert receipt.provider_message_id == "om_message_1"
    assert captured["method"] == "PUT"
    assert captured["url"].endswith("/im/v1/messages/om_message_1")
    assert captured["headers"]["Content-type"] == "application/json; charset=utf-8"
    assert json.loads(captured["body"]["content"]) == {"text": "partial answer"}


def test_stream_flush_preserves_feishu_http_error_body(settings_factory, tmp_path, monkeypatch):
    channel = _channel(settings_factory, tmp_path)
    monkeypatch.setattr(channel, "fetch_access_token", lambda: "tok")

    def fake_urlopen(req, timeout):
        payload = json.dumps({"code": 230001, "msg": "invalid request parameter"}).encode("utf-8")
        raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", None, BytesIO(payload))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    msg = IncomingMessage(channel_type="feishu", user_id="ou_x", content="q", msg_id="m1")

    receipt = channel._flush_stream_message(msg, "partial answer", "om_message_1")

    assert receipt.delivered is False
    assert receipt.retryable is False
    assert receipt.error == "230001: invalid request parameter"


def test_fetch_access_token_parses_token(settings_factory, tmp_path, monkeypatch):
    channel = _channel(settings_factory, tmp_path)
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout: _Resp({"code": 0, "tenant_access_token": "t-abc"}),
    )
    assert channel.fetch_access_token() == "t-abc"


def test_fetch_access_token_error_code_returns_empty(settings_factory, tmp_path, monkeypatch):
    channel = _channel(settings_factory, tmp_path)
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout: _Resp({"code": 99, "msg": "bad"}),
    )
    assert channel.fetch_access_token() == ""


def test_fetch_access_token_network_error_returns_empty(settings_factory, tmp_path, monkeypatch):
    channel = _channel(settings_factory, tmp_path)

    def boom(req, timeout):
        raise OSError("network down")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    assert channel.fetch_access_token() == ""


def test_startup_supervisor_rebuilds_client_on_failure(settings_factory, tmp_path, monkeypatch):
    """A dying ws client must be rebuilt by the supervisor loop until stop()."""
    channel = _channel(settings_factory, tmp_path)
    channel.start_consuming = lambda: None  # avoid starting a real consume thread
    monkeypatch.setattr("channel.feishu.feishu_channel.RECONNECT_MIN_BACKOFF", 0.0)
    monkeypatch.setattr("channel.feishu.feishu_channel.RECONNECT_MAX_BACKOFF", 0.0)

    starts = {"count": 0}

    class FakeWsClient:
        def __init__(self, *a, **kw):
            pass

        def start(self):
            starts["count"] += 1
            if starts["count"] >= 3:
                channel._supervise = False  # simulate stop() after a few retries
            raise RuntimeError("keepalive ping timeout")

    class FakeBuilder:
        def register_p2_im_message_receive_v1(self, _fn):
            return self

        def register_p2_card_action_trigger(self, _fn):
            return self

        def build(self):
            return object()

    fake_lark = types.SimpleNamespace(
        JSON=types.SimpleNamespace(marshal=lambda d: "{}"),
        EventDispatcherHandler=types.SimpleNamespace(builder=lambda a, b: FakeBuilder()),
        ws=types.SimpleNamespace(Client=FakeWsClient),
        LogLevel=types.SimpleNamespace(WARNING=1),
    )
    monkeypatch.setitem(sys.modules, "lark_oapi", fake_lark)

    channel.startup()

    # start() raised every time; supervisor kept rebuilding until _supervise
    # was cleared. Proves the channel self-heals instead of dying silently.
    assert starts["count"] == 3


def test_stop_breaks_supervisor(settings_factory, tmp_path):
    channel = _channel(settings_factory, tmp_path)
    assert channel._supervise is True
    channel.stop()
    assert channel._supervise is False
