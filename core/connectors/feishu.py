"""Feishu WebSocket long-connection connector using lark-oapi SDK."""

import json
from typing import Callable, Optional

import lark_oapi as lark
from loguru import logger

from core.config import settings


class FeishuClient:
    """Feishu bot client with WebSocket long-connection support."""

    def __init__(self, on_message: Optional[Callable] = None):
        self._on_message_callback = on_message
        self._client = lark.Client.builder() \
            .app_id(settings.feishu_app_id) \
            .app_secret(settings.feishu_app_secret) \
            .log_level(lark.LogLevel.DEBUG if settings.debug else lark.LogLevel.INFO) \
            .build()

    @property
    def client(self) -> lark.Client:
        return self._client

    def start_ws(self) -> None:
        """Start WebSocket long-connection to receive events."""
        from lark_oapi.ws import Client as WsClient

        event_handler = lark.EventDispatcherHandler.builder(
            settings.feishu_verification_token,
            settings.feishu_encrypt_key,
        ).register_p2_im_message_receive_v1(self._handle_message_event).build()

        ws_client = WsClient(
            settings.feishu_app_id,
            settings.feishu_app_secret,
            event_handler=event_handler,
            log_level=lark.LogLevel.DEBUG if settings.debug else lark.LogLevel.INFO,
        )

        logger.info("Starting Feishu WebSocket connection...")
        ws_client.start()

    def _handle_message_event(self, data) -> None:
        """Handle im.message.receive_v1 event."""
        try:
            event = data.event
            message = event.message
            sender = event.sender

            message_id = message.message_id
            chat_id = message.chat_id
            chat_type = message.chat_type  # p2p or group
            message_type = message.message_type
            content_raw = message.content
            sender_id = sender.sender_id.open_id if sender.sender_id else ""
            sender_type = sender.sender_type  # user or bot

            # Ignore messages from bots (including self)
            if sender_type == "bot":
                logger.debug("Ignoring bot message from {}", sender_id)
                return

            # Parse content
            content = ""
            if message_type == "text":
                try:
                    content = json.loads(content_raw).get("text", "")
                except (json.JSONDecodeError, TypeError):
                    content = content_raw or ""

            # For group messages, check if bot is mentioned
            mentions = message.mentions or []
            is_mentioned = any(
                m.name == settings.feishu_bot_name for m in mentions
            ) if mentions else False

            # Thread fields — non-empty when message is inside a thread/topic
            thread_id = getattr(message, "thread_id", "") or ""
            root_id = getattr(message, "root_id", "") or ""
            parent_id_val = getattr(message, "parent_id", "") or ""

            event_data = {
                "platform": "feishu",
                "platform_message_id": message_id,
                "chat_id": chat_id,
                "chat_type": chat_type,
                "sender_id": sender_id,
                "sender_type": sender_type,
                "sender_name": "",  # Will be enriched later if needed
                "message_type": message_type,
                "content": content,
                "content_raw": content_raw,
                "is_mentioned": is_mentioned,
                "mentions": [{"key": m.key, "id": m.id.open_id, "name": m.name} for m in mentions] if mentions else [],
                "thread_id": thread_id,
                "root_id": root_id,
                "parent_id": parent_id_val,
                "raw_payload": json.dumps(data.__dict__, default=str),
            }

            logger.info(
                "Received message: chat_id={}, sender={}, type={}, content_preview={}",
                chat_id, sender_id, message_type, content[:50] if content else ""
            )

            if self._on_message_callback:
                self._on_message_callback(event_data)

        except Exception as e:
            logger.exception("Failed to handle Feishu message event: {}", e)

    def send_message(self, receive_id: str, content: str, receive_id_type: str = "chat_id", msg_type: str = "text") -> bool:
        """Send a message to a chat or user."""
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

        if msg_type == "text":
            body_content = json.dumps({"text": content})
        else:
            body_content = content

        request = CreateMessageRequest.builder() \
            .receive_id_type(receive_id_type) \
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(receive_id)
                .msg_type(msg_type)
                .content(body_content)
                .build()
            ).build()

        response = self._client.im.v1.message.create(request)

        if not response.success():
            logger.error(
                "Failed to send message: code={}, msg={}",
                response.code, response.msg
            )
            return False

        logger.info("Message sent to {}", receive_id)
        return True

    def reply_message(self, message_id: str, content: str, msg_type: str = "text",
                      reply_in_thread: bool = False) -> dict | None:
        """Reply to a specific message.

        Args:
            reply_in_thread: If True, creates a thread/topic on first reply,
                             or replies inside an existing thread.

        Returns:
            dict with message_id and thread_id from response, or None on failure.
        """
        from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody

        if msg_type == "text":
            body_content = json.dumps({"text": content})
        else:
            body_content = content

        body_builder = ReplyMessageRequestBody.builder() \
            .msg_type(msg_type) \
            .content(body_content)
        if reply_in_thread:
            body_builder = body_builder.reply_in_thread(True)

        request = ReplyMessageRequest.builder() \
            .message_id(message_id) \
            .request_body(body_builder.build()) \
            .build()

        response = self._client.im.v1.message.reply(request)

        if not response.success():
            logger.error(
                "Failed to reply message: code={}, msg={}",
                response.code, response.msg
            )
            return None

        resp_data = response.data
        result = {
            "message_id": getattr(resp_data, "message_id", "") or "",
            "thread_id": getattr(resp_data, "thread_id", "") or "",
            "root_id": getattr(resp_data, "root_id", "") or "",
        }
        logger.info("Replied to message {} (thread_id={}, reply_in_thread={})",
                     message_id, result["thread_id"], reply_in_thread)
        return result
