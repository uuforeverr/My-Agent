import json
import time
import urllib.request
from typing import Optional

from agent.agent import Agent
from channel.chat_channel import ChatChannel
from channel.message import IncomingMessage
from channel.reply import Reply
from config import Settings
from tools.approval import handle_approval_command

TENANT_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal/"
SEND_MESSAGE_URL = "https://open.feishu.cn/open-apis/im/v1/messages"
BOT_INFO_URL = "https://open.feishu.cn/open-apis/bot/v3/info/"
STALE_MESSAGE_SECONDS = 60
DEDUP_CAPACITY = 2000
RECONNECT_MIN_BACKOFF = 1.0
RECONNECT_MAX_BACKOFF = 30.0


class FeishuChannel(ChatChannel):
    channel_type = "feishu"

    def __init__(self, agent: Agent, settings: Settings, **kwargs):
        super().__init__(agent, settings, **kwargs)
        self.app_id = settings.feishu_app_id
        self.app_secret = settings.feishu_app_secret
        self._seen_msg_ids: dict[str, float] = {}
        self._ws_client = None
        self._supervise = True
        self._bot_open_id: Optional[str] = None

    def startup(self) -> None:  # pragma: no cover - real websocket connection, excluded from offline tests
        if not self.app_id or not self.app_secret:
            raise RuntimeError("FEISHU_APP_ID / FEISHU_APP_SECRET missing; cannot start feishu channel")
        import lark_oapi as lark

        self._fetch_bot_open_id()
        self.start_consuming()

        def _on_message(data) -> None:
            event = json.loads(lark.JSON.marshal(data)).get("event", {})
            self._handle_message_event(event)

        handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(_on_message)
            .build()
        )

        # Supervisor loop: lark's built-in auto_reconnect gives up after a
        # bounded number of retries and then lets the receive loop die silently,
        # leaving start() blocked forever with a dead socket (observed as
        # "receive message loop exit, err: keepalive ping timeout" / "no close
        # frame received"). We wrap start() so that whenever it returns or raises
        # we rebuild the client and reconnect with exponential backoff.
        backoff = RECONNECT_MIN_BACKOFF
        while self._supervise:
            try:
                # lark.ws.client keeps the asyncio event loop in a MODULE-LEVEL
                # global. After a start() aborts, that loop is closed/broken, so
                # simply calling start() again on a fresh client reuses the dead
                # loop and fails instantly ("Event loop is closed"). Install a
                # brand-new loop before each attempt so reconnection truly works.
                self._install_fresh_event_loop()
                self._ws_client = lark.ws.Client(
                    self.app_id,
                    self.app_secret,
                    event_handler=handler,
                    log_level=lark.LogLevel.WARNING,
                )
                self._ws_client.start()
            except Exception:
                pass
            if not self._supervise:
                break
            time.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_MAX_BACKOFF)

    @staticmethod
    def _install_fresh_event_loop() -> None:  # pragma: no cover - asyncio/lark plumbing
        import asyncio

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            import lark_oapi.ws.client as _lark_ws_client_mod
            _lark_ws_client_mod.loop = loop
        except Exception:
            pass

    def _handle_message_event(self, event: dict) -> None:
        message = event.get("message") or {}
        sender = event.get("sender") or {}
        if not message or not sender:
            return

        msg_id = message.get("message_id")
        if not msg_id or self._is_duplicate(msg_id):
            return

        if self._is_stale(message.get("create_time")):
            return

        if message.get("message_type") != "text":
            return

        chat_type = message.get("chat_type")
        is_group = chat_type == "group"
        if is_group and not self._is_mention_bot(message.get("mentions")):
            return

        content = self._extract_text(message.get("content"))
        if not content:
            return

        open_id = (sender.get("sender_id") or {}).get("open_id") or ""
        if not open_id:
            return

        incoming = IncomingMessage(
            channel_type=self.channel_type,
            user_id=open_id,
            content=content,
            msg_id=msg_id,
            is_group=is_group,
            group_id=message.get("chat_id") if is_group else None,
            raw=event,
        )
        if content.strip() == "/summarize":
            summarize = getattr(self.agent, "summarize_session", None)
            reply_text = summarize(incoming.session_id) if callable(summarize) else "无需压缩"
            self.send(Reply(reply_text), incoming)
            return

        handled = handle_approval_command(self.agent, content, open_id)
        if handled is not None:
            self.send(Reply(handled), incoming)
            return

        self.produce(incoming)

    def _fetch_bot_open_id(self) -> None:
        access_token = self.fetch_access_token()
        if not access_token:
            return
        req = urllib.request.Request(
            url=BOT_INFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception:
            return
        if data.get("code") == 0:
            self._bot_open_id = (data.get("bot") or {}).get("open_id")

    def _is_mention_bot(self, mentions) -> bool:
        # Once "read all group messages" is granted, Feishu delivers every group
        # message, including ones that @ someone else. Only treat a group message
        # as directed at us when a mention actually resolves to the bot's open_id.
        if not mentions:
            return False
        if self._bot_open_id:
            return any(
                (m.get("id") or {}).get("open_id") == self._bot_open_id
                for m in mentions
            )
        # No bot open_id resolved (e.g. bot-info fetch failed). Fall back to the
        # legacy assumption that any mention means the bot, so we do not go silent.
        return True

    def _is_duplicate(self, msg_id: str) -> bool:
        if msg_id in self._seen_msg_ids:
            return True
        if len(self._seen_msg_ids) >= DEDUP_CAPACITY:
            self._seen_msg_ids.clear()
        self._seen_msg_ids[msg_id] = time.time()
        return False

    @staticmethod
    def _is_stale(create_time) -> bool:
        if not create_time:
            return False
        try:
            age = time.time() - int(create_time) / 1000
        except (TypeError, ValueError):
            return False
        return age > STALE_MESSAGE_SECONDS

    @staticmethod
    def _extract_text(raw_content) -> str:
        if not raw_content:
            return ""
        try:
            parsed = json.loads(raw_content)
        except (TypeError, ValueError):
            return ""
        text = parsed.get("text", "") if isinstance(parsed, dict) else ""
        return text.replace("@_user_1", "").strip()

    def send(self, reply: Reply, message: IncomingMessage) -> None:
        access_token = self.fetch_access_token()
        if not access_token:
            return
        if message.is_group and message.group_id:
            receive_id, receive_id_type = message.group_id, "chat_id"
        else:
            receive_id, receive_id_type = message.user_id, "open_id"

        payload = {
            "receive_id": receive_id,
            "msg_type": "text",
            "content": json.dumps({"text": reply.content}, ensure_ascii=False),
        }
        req = urllib.request.Request(
            url=f"{SEND_MESSAGE_URL}?receive_id_type={receive_id_type}",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                json.loads(resp.read().decode("utf-8"))
        except Exception:
            return

    def fetch_access_token(self) -> Optional[str]:
        req = urllib.request.Request(
            url=TENANT_TOKEN_URL,
            data=json.dumps({"app_id": self.app_id, "app_secret": self.app_secret}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception:
            return ""
        if data.get("code") != 0:
            return ""
        return data.get("tenant_access_token", "")

    def stop(self) -> None:
        super().stop()
        self._supervise = False
        self._ws_client = None
