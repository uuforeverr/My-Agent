import json
import re
import threading
import time
import urllib.error
import urllib.request
from typing import Optional

from agent.agent import Agent
from channel.chat_channel import ChatChannel
from channel.delivery import DeliveryReceipt
from channel.message import IncomingMessage
from channel.reply import Reply
from channel.streaming import StreamFlushWorker
from config import Settings
from tools.approval import handle_approval_command

TENANT_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal/"
SEND_MESSAGE_URL = "https://open.feishu.cn/open-apis/im/v1/messages"
UPDATE_MESSAGE_URL = "https://open.feishu.cn/open-apis/im/v1/messages"
BOT_INFO_URL = "https://open.feishu.cn/open-apis/bot/v3/info/"
STALE_MESSAGE_SECONDS = 60
DEDUP_CAPACITY = 2000
RECONNECT_MIN_BACKOFF = 1.0
RECONNECT_MAX_BACKOFF = 30.0
APPROVAL_CARD_SOURCE = "my-agent-tool-approval"
APPROVAL_CARD_CACHE_TTL = 25 * 3600
APPROVAL_ARGUMENT_LIMIT = 3500


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
        self._flush_worker: Optional[StreamFlushWorker] = None
        self._flush_worker_lock = threading.Lock()
        self._approval_cards: dict[str, tuple[float, dict]] = {}
        self._approval_cards_lock = threading.Lock()

    def startup(self) -> None:  # pragma: no cover - real websocket connection, excluded from offline tests
        if not self.app_id or not self.app_secret:
            raise RuntimeError("FEISHU_APP_ID / FEISHU_APP_SECRET missing; cannot start feishu channel")
        import lark_oapi as lark

        self._fetch_bot_open_id()
        self.start_consuming()

        def _on_message(data) -> None:
            event = json.loads(lark.JSON.marshal(data)).get("event", {})
            self._handle_message_event(event)

        def _on_card_action(data):
            from lark_oapi.event.callback.model.p2_card_action_trigger import (
                P2CardActionTriggerResponse,
            )

            event = json.loads(lark.JSON.marshal(data)).get("event", {})
            return P2CardActionTriggerResponse(self._handle_card_action_event(event))

        handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(_on_message)
            .register_p2_card_action_trigger(_on_card_action)
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
        app_received_at_ns = time.time_ns()
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

        group_id = message.get("chat_id") if is_group else None
        scope = group_id if is_group and group_id else open_id
        if content.strip() == "/new":
            self.start_new_conversation(scope)
            incoming = IncomingMessage(
                channel_type=self.channel_type,
                user_id=open_id,
                content=content,
                msg_id=msg_id,
                is_group=is_group,
                group_id=group_id,
                raw=event,
            )
            self.send(Reply("🌱 已开启新对话"), incoming)
            return

        incoming = IncomingMessage(
            channel_type=self.channel_type,
            user_id=open_id,
            content=content,
            msg_id=msg_id,
            is_group=is_group,
            group_id=group_id,
            conversation_id=self.current_conversation(scope),
            raw=event,
            received_at_ns=app_received_at_ns,
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

    def _send_event(self, event: dict, message: IncomingMessage) -> DeliveryReceipt:
        if event.get("type") != "approval_request":
            return super()._send_event(event, message)
        return self._send_approval_with_fallback(event, message)

    def _send_approval_with_fallback(
        self,
        event: dict,
        message: IncomingMessage,
    ) -> DeliveryReceipt:
        receipt = self._send_approval_card(event, message)
        if receipt.delivered:
            return receipt
        # A missing callback configuration or an older Feishu client must not
        # strand the tool call. Preserve the slash-command prompt as a fallback.
        content = str(event.get("content") or "").strip()
        if content:
            return self._send_text(content, message)
        return receipt

    def _send_approval_card(
        self,
        event: dict,
        message: IncomingMessage,
    ) -> DeliveryReceipt:
        approval_id = str(event.get("approval_id") or "").strip()
        if not approval_id:
            return DeliveryReceipt.failed("approval event missing approval_id")
        normalized = self._normalize_approval_event(event)
        self._remember_approval_card(approval_id, normalized)
        return self._send_payload(
            "interactive",
            json.dumps(self._build_approval_card(normalized), ensure_ascii=False),
            message,
        )

    def _handle_card_action_event(self, event: dict) -> dict:
        action = event.get("action") or {}
        value = action.get("value") or {}
        if not isinstance(value, dict) or value.get("source") != APPROVAL_CARD_SOURCE:
            return {"toast": {"type": "warning", "content": "无法识别该审批操作"}}

        approval_id = str(value.get("approval_id") or "").strip()
        operator_id = str((event.get("operator") or {}).get("open_id") or "").strip()
        requested_action = str(value.get("action") or "").strip().lower()
        scope = "session" if value.get("scope") == "session" else "once"
        if not approval_id or not operator_id:
            return {"toast": {"type": "error", "content": "审批信息不完整"}}

        if requested_action == "approve":
            handler = getattr(self.agent, "approve_tool", None)
            message = (
                handler(approval_id, operator_id, scope)
                if callable(handler)
                else "审批通道不可用"
            )
            resolved_status = "approved"
        elif requested_action == "deny":
            handler = getattr(self.agent, "deny_tool", None)
            message = (
                handler(approval_id, operator_id)
                if callable(handler)
                else "审批通道不可用"
            )
            resolved_status = "denied"
        else:
            return {"toast": {"type": "warning", "content": "无法识别该审批操作"}}

        message_text = str(message)
        succeeded = message_text.startswith(("已批准", "已拒绝"))
        terminal_failure = message_text in {"审批已超时", "该审批已处理"} or message_text.startswith(
            "未找到待审批操作"
        )
        if succeeded:
            card_event = self._get_approval_card(approval_id)
            context = event.get("context") or {}
            message_id = str(context.get("open_message_id") or "").strip()
            if card_event is not None and message_id:
                card = self._build_approval_card(
                    card_event,
                    status=resolved_status,
                    result=message_text,
                )
                self._queue_approval_card_update(message_id, card)
            self._forget_approval_card(approval_id)
        elif terminal_failure:
            card_event = self._get_approval_card(approval_id)
            context = event.get("context") or {}
            message_id = str(context.get("open_message_id") or "").strip()
            if card_event is not None and message_id:
                card = self._build_approval_card(
                    card_event,
                    status="expired",
                    result=message_text,
                )
                self._queue_approval_card_update(message_id, card)
            self._forget_approval_card(approval_id)
        return {
            "toast": {
                "type": "success" if succeeded else "error",
                "content": message_text,
            }
        }

    def _queue_approval_card_update(self, message_id: str, card: dict) -> None:
        threading.Thread(
            target=self._update_interactive_message,
            args=(message_id, card),
            daemon=True,
            name=f"feishu-approval-update-{message_id[-8:]}",
        ).start()

    def _update_interactive_message(self, message_id: str, card: dict) -> DeliveryReceipt:
        access_token = self.fetch_access_token()
        if not access_token:
            return DeliveryReceipt.failed("failed to obtain Feishu access token", retryable=True)
        payload = {
            "msg_type": "interactive",
            "content": json.dumps(card, ensure_ascii=False),
        }
        req = urllib.request.Request(
            url=f"{UPDATE_MESSAGE_URL}/{message_id}",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            method="PUT",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return self._http_error_receipt(exc, "Feishu approval card update failed")
        except Exception as exc:
            return DeliveryReceipt.failed(f"{type(exc).__name__}: {exc}", retryable=True)
        if data.get("code") != 0:
            code = data.get("code")
            return DeliveryReceipt.failed(
                f"{code}: {data.get('msg') or data.get('message') or 'Feishu approval card update failed'}",
                retryable=code in {230020, 11232, 18121, 99991663},
            )
        return DeliveryReceipt.success(message_id)

    def _remember_approval_card(self, approval_id: str, event: dict) -> None:
        now = time.monotonic()
        with self._approval_cards_lock:
            self._approval_cards = {
                key: item
                for key, item in self._approval_cards.items()
                if now - item[0] < APPROVAL_CARD_CACHE_TTL
            }
            self._approval_cards[approval_id] = (now, dict(event))

    def _get_approval_card(self, approval_id: str) -> Optional[dict]:
        with self._approval_cards_lock:
            item = self._approval_cards.get(approval_id)
            return dict(item[1]) if item is not None else None

    def _forget_approval_card(self, approval_id: str) -> None:
        with self._approval_cards_lock:
            self._approval_cards.pop(approval_id, None)

    @staticmethod
    def _normalize_approval_event(event: dict) -> dict:
        content = str(event.get("content") or "")
        approval_id = str(event.get("approval_id") or "").strip()
        tool_name = str(event.get("tool_name") or "").strip()
        risk_level = str(event.get("risk_level") or "").strip()
        argument_lines = str(event.get("argument_lines") or "").strip()

        if not approval_id:
            match = re.search(r"待审批\s*\[([^\]]+)\]", content)
            approval_id = match.group(1).strip() if match else ""
        if not tool_name or not risk_level:
            match = re.search(r"工具:\s*(\S+)\s+等级:\s*(\S+)", content)
            if match:
                tool_name = tool_name or match.group(1)
                risk_level = risk_level or match.group(2)
        if not argument_lines:
            match = re.search(r"参数:\s*\n(.*?)(?:\n\s*\n/approve|\Z)", content, re.S)
            argument_lines = match.group(1).strip() if match else "(none)"
        argument_lines = argument_lines.replace("```", "''' ")
        if len(argument_lines) > APPROVAL_ARGUMENT_LIMIT:
            argument_lines = argument_lines[:APPROVAL_ARGUMENT_LIMIT] + "\n…（参数已截断）"

        timeout_seconds = event.get("timeout_seconds")
        timeout_text = "120s"
        try:
            seconds = float(timeout_seconds)
            timeout_text = f"{int(seconds)}s" if seconds == int(seconds) else f"{seconds}s"
        except (TypeError, ValueError):
            match = re.search(r"超时\s+([^\s]+)\s+将自动拒绝", content)
            if match:
                timeout_text = match.group(1)
        return {
            "type": "approval_request",
            "content": content,
            "approval_id": approval_id,
            "tool_name": tool_name or "unknown",
            "risk_level": risk_level or "unknown",
            "argument_lines": argument_lines or "(none)",
            "timeout_text": timeout_text,
            "session_scope_allowed": bool(event.get("session_scope_allowed", tool_name != "bash")),
        }

    @staticmethod
    def _build_approval_card(event: dict, *, status: str = "pending", result: str = "") -> dict:
        approval_id = str(event.get("approval_id") or "")
        tool_name = str(event.get("tool_name") or "unknown")
        risk_level = str(event.get("risk_level") or "unknown")
        argument_lines = str(event.get("argument_lines") or "(none)")
        timeout_text = str(event.get("timeout_text") or "120s")
        is_pending = status == "pending"
        is_approved = status == "approved"
        is_denied = status == "denied"
        header_template = "yellow" if is_pending else ("green" if is_approved else ("red" if is_denied else "grey"))
        title = "工具操作待审批" if is_pending else ("工具操作已批准" if is_approved else ("工具操作已拒绝" if is_denied else "工具审批已失效"))
        tag_text = "待处理" if is_pending else ("已批准" if is_approved else ("已拒绝" if is_denied else "已失效"))
        tag_color = "yellow" if is_pending else ("green" if is_approved else ("red" if is_denied else "neutral"))

        elements = [
            {
                "tag": "column_set",
                "flex_mode": "none",
                "margin": "0px 0px 12px 0px",
                "columns": [
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 1,
                        "background_style": "grey-50",
                        "padding": "12px",
                        "vertical_spacing": "8px",
                        "elements": [
                            {
                                "tag": "markdown",
                                "content": (
                                    f"**审批编号**  `{approval_id}`\n"
                                    f"**工具**  `{tool_name}`    **风险等级**  `{risk_level}`"
                                ),
                            },
                            {
                                "tag": "markdown",
                                "content": f"**参数预览**\n```text\n{argument_lines}\n```",
                            },
                        ],
                    }
                ],
            }
        ]

        if is_pending:
            elements.append(
                {
                    "tag": "markdown",
                    "content": "**请选择如何处理本次工具调用**",
                    "text_size": "heading-4",
                    "margin": "0px 0px 8px 0px",
                }
            )
            buttons = [("仅本次允许", "primary_filled", "approve", "once")]
            if event.get("session_scope_allowed", True):
                buttons.append(("本会话允许", "default", "approve", "session"))
            buttons.append(("拒绝", "danger", "deny", "once"))
            elements.append(
                {
                    "tag": "column_set",
                    "flex_mode": "none",
                    "horizontal_spacing": "8px",
                    "margin": "0px 0px 12px 0px",
                    "columns": [
                        {
                            "tag": "column",
                            "width": "weighted",
                            "weight": 1,
                            "elements": [
                                {
                                    "tag": "button",
                                    "text": {"tag": "plain_text", "content": label},
                                    "type": button_type,
                                    "width": "fill",
                                    "behaviors": [
                                        {
                                            "type": "callback",
                                            "value": {
                                                "source": APPROVAL_CARD_SOURCE,
                                                "approval_id": approval_id,
                                                "action": action,
                                                "scope": scope,
                                            },
                                        }
                                    ],
                                }
                            ],
                        }
                        for label, button_type, action, scope in buttons
                    ],
                }
            )
            elements.append(
                {
                    "tag": "markdown",
                    "content": (
                        f"<font color='grey'>若未操作，{timeout_text} 后自动拒绝。"
                        "仍可使用 /approve 或 /deny 作为兼容方式。</font>"
                    ),
                    "text_size": "caption",
                }
            )
        else:
            result_color = "green-50" if is_approved else ("red-50" if is_denied else "grey-50")
            elements.append(
                {
                    "tag": "column_set",
                    "flex_mode": "none",
                    "columns": [
                        {
                            "tag": "column",
                            "width": "weighted",
                            "weight": 1,
                            "background_style": result_color,
                            "padding": "12px",
                            "elements": [
                                {
                                    "tag": "markdown",
                                    "content": f"**{result or tag_text}**",
                                    "text_size": "heading-4",
                                }
                            ],
                        }
                    ],
                }
            )

        return {
            "schema": "2.0",
            "config": {
                "update_multi": True,
                "width_mode": "default",
                "enable_forward": False,
                "summary": {"content": f"{title}：{tool_name}"},
                "style": {
                    "text_size": {
                        "caption": {
                            "default": "notation",
                            "pc": "notation",
                            "mobile": "notation",
                        }
                    }
                },
            },
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "subtitle": {"tag": "plain_text", "content": f"{tool_name} · {risk_level}"},
                "template": header_template,
                "icon": {"tag": "standard_icon", "token": "approval_colorful"},
                "text_tag_list": [
                    {
                        "tag": "text_tag",
                        "text": {"tag": "plain_text", "content": tag_text},
                        "color": tag_color,
                    }
                ],
            },
            "body": {
                "direction": "vertical",
                "padding": "12px 12px 20px 12px",
                "vertical_spacing": "0px",
                "elements": elements,
            },
        }

    def send(self, reply: Reply, message: IncomingMessage) -> DeliveryReceipt:
        if reply.type == "approval":
            event = self._normalize_approval_event({"content": reply.content})
            if event.get("approval_id"):
                return self._send_approval_with_fallback(event, message)
        return self._send_text(reply.content, message)

    def _send_text(self, content: str, message: IncomingMessage) -> DeliveryReceipt:
        return self._send_payload(
            "text",
            json.dumps({"text": content}, ensure_ascii=False),
            message,
        )

    def _send_payload(
        self,
        msg_type: str,
        content: str,
        message: IncomingMessage,
    ) -> DeliveryReceipt:
        access_token = self.fetch_access_token()
        if not access_token:
            return DeliveryReceipt.failed("failed to obtain Feishu access token", retryable=True)
        if message.is_group and message.group_id:
            receive_id, receive_id_type = message.group_id, "chat_id"
        else:
            receive_id, receive_id_type = message.user_id, "open_id"

        payload = {
            "receive_id": receive_id,
            "msg_type": msg_type,
            "content": content,
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
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            # Feishu message creation is not idempotent. Once the request may
            # have left this process, a timeout is ambiguous rather than safely
            # retryable.
            return DeliveryReceipt.unknown(f"{type(exc).__name__}: {exc}")
        if data.get("code") != 0:
            code = data.get("code")
            message_text = data.get("msg") or data.get("message") or "Feishu send failed"
            retryable = code in {11232, 18121, 99991663}
            return DeliveryReceipt.failed(f"{code}: {message_text}", retryable=retryable)
        message_id = str((data.get("data") or {}).get("message_id") or data.get("message_id") or "")
        return DeliveryReceipt.success(message_id)

    def _open_stream(self, message: IncomingMessage):
        with self._flush_worker_lock:
            if self._flush_worker is None:
                self._flush_worker = StreamFlushWorker(
                    interval=self.settings.feishu_stream_flush_interval,
                    char_threshold=self.settings.feishu_stream_flush_chars,
                )
            worker = self._flush_worker
        return worker.open(
            lambda content, provider_message_id, final: self._flush_stream_message(
                message,
                content,
                provider_message_id,
            ),
            channel_type=self.channel_type,
        )

    def _flush_stream_message(
        self,
        message: IncomingMessage,
        content: str,
        provider_message_id: str,
    ) -> DeliveryReceipt:
        if not provider_message_id:
            return self.send(Reply(content or "正在处理…"), message)
        access_token = self.fetch_access_token()
        if not access_token:
            return DeliveryReceipt.failed("failed to obtain Feishu access token", retryable=True)
        payload = {
            "msg_type": "text",
            "content": json.dumps({"text": content}, ensure_ascii=False),
        }
        req = urllib.request.Request(
            url=f"{UPDATE_MESSAGE_URL}/{provider_message_id}",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            # Feishu's ordinary text/post message edit API is PUT. PATCH is
            # reserved for the separate interactive-card update API.
            method="PUT",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return self._http_error_receipt(exc, "Feishu update failed")
        except Exception as exc:
            return DeliveryReceipt.failed(f"{type(exc).__name__}: {exc}", retryable=True)
        if data.get("code") != 0:
            code = data.get("code")
            return DeliveryReceipt.failed(
                f"{code}: {data.get('msg') or data.get('message') or 'Feishu update failed'}",
                retryable=code in {230020, 11232, 18121, 99991663},
            )
        return DeliveryReceipt.success(provider_message_id)

    @staticmethod
    def _http_error_receipt(exc: urllib.error.HTTPError, fallback: str) -> DeliveryReceipt:
        """Preserve Feishu's structured error instead of reducing it to HTTP 400."""
        data = {}
        try:
            raw = exc.read().decode("utf-8", errors="replace")
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                data = parsed
        except (OSError, ValueError, TypeError):
            pass
        code = data.get("code")
        message = data.get("msg") or data.get("message") or fallback
        label = str(code) if code is not None else f"HTTP {exc.code}"
        retryable = exc.code >= 500 or code in {230020, 11232, 18121, 99991663}
        return DeliveryReceipt.failed(f"{label}: {message}", retryable=retryable)

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
        with self._flush_worker_lock:
            worker = self._flush_worker
            self._flush_worker = None
        if worker is not None:
            worker.stop()
        self._supervise = False
        self._ws_client = None
