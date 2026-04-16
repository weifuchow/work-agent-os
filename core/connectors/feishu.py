"""Feishu WebSocket long-connection connector using lark-oapi SDK."""

import json
import os
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

    def react_to_message(self, message_id: str, emoji: str = "GLANCE") -> bool:
        """Add an emoji reaction to a message.

        Args:
            message_id: The Feishu message ID.
            emoji: Emoji type string (e.g. "Eyes", "Thumbsup", "Done").

        Returns True on success.
        """
        from lark_oapi.api.im.v1 import (
            CreateMessageReactionRequest,
            CreateMessageReactionRequestBody,
        )

        request = CreateMessageReactionRequest.builder() \
            .message_id(message_id) \
            .request_body(
                CreateMessageReactionRequestBody.builder()
                .reaction_type({"emoji_type": emoji})
                .build()
            ).build()

        response = self._client.im.v1.message_reaction.create(request)
        if not response.success():
            logger.warning("Failed to react to message {}: code={}, msg={}",
                           message_id, response.code, response.msg)
            return False
        logger.debug("Reacted {} to message {}", emoji, message_id)
        return True

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
            # Log ALL sender_type values to diagnose double-reply issue
            logger.info("Message event: sender_type={!r} sender_id={!r} msg_id={}",
                        sender_type, sender_id, message_id)
            if sender_type in ("bot", "app"):
                logger.info("Ignoring bot/app message from {} (type={})", sender_id, sender_type)
                return

            # Parse content — support multimodal types
            content, media_info = _parse_message_content(message_type, content_raw)

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
                "media_info": media_info,
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

    def send_message(self, receive_id: str, content: str, receive_id_type: str = "chat_id", msg_type: str = "text") -> dict | None:
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
            return None

        resp_data = response.data
        result = {
            "message_id": getattr(resp_data, "message_id", "") or "",
            "thread_id": getattr(resp_data, "thread_id", "") or "",
            "root_id": getattr(resp_data, "root_id", "") or "",
        }
        logger.info("Message sent to {} (message_id={})", receive_id, result["message_id"])
        return result

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

    def upload_image(self, image_path: str) -> str | None:
        """Upload a local image file and return image_key."""
        from lark_oapi.api.im.v1 import CreateImageRequest, CreateImageRequestBody

        try:
            with open(image_path, "rb") as fp:
                request = CreateImageRequest.builder() \
                    .request_body(
                        CreateImageRequestBody.builder()
                        .image_type("message")
                        .image(fp)
                        .build()
                    ).build()
                response = self._client.im.v1.image.create(request)
        except Exception as e:
            logger.error("Failed to upload image {}: {}", image_path, e)
            return None

        if not response.success():
            logger.error(
                "Failed to upload image: code={}, msg={}",
                response.code, response.msg
            )
            return None

        image_key = getattr(response.data, "image_key", "") or ""
        if image_key:
            logger.info("Image uploaded: {} -> {}", image_path, image_key)
        return image_key or None

    def get_image_bytes(
        self,
        image_key: str,
        *,
        message_id: str | None = None,
        resource_type: str = "image",
    ) -> tuple[bytes, str | None] | None:
        """Download image bytes by image_key."""
        from lark_oapi.api.im.v1 import GetImageRequest
        from lark_oapi.api.im.v1 import GetMessageResourceRequest

        request = GetImageRequest.builder().image_key(image_key).build()
        response = self._client.im.v1.image.get(request)

        if getattr(response, "code", -1) != 0 or not getattr(response, "file", None):
            if message_id:
                fallback_request = (
                    GetMessageResourceRequest.builder()
                    .message_id(message_id)
                    .file_key(image_key)
                    .type(resource_type)
                    .build()
                )
                fallback_response = self._client.im.v1.message_resource.get(fallback_request)
                if getattr(fallback_response, "code", -1) == 0 and getattr(fallback_response, "file", None):
                    file_obj = fallback_response.file
                    file_obj.seek(0)
                    return file_obj.read(), getattr(fallback_response, "file_name", None)

            logger.error("Failed to get image {}: code={} msg={}",
                         image_key, getattr(response, "code", ""), getattr(response, "msg", ""))
            return None

        file_obj = response.file
        file_obj.seek(0)
        return file_obj.read(), getattr(response, "file_name", None)

    def get_file_bytes(
        self,
        file_key: str,
        *,
        message_id: str | None = None,
        resource_type: str = "file",
    ) -> tuple[bytes, str | None] | None:
        """Download file bytes by file_key."""
        from lark_oapi.api.im.v1 import GetFileRequest
        from lark_oapi.api.im.v1 import GetMessageResourceRequest

        request = GetFileRequest.builder().file_key(file_key).build()
        response = self._client.im.v1.file.get(request)

        if getattr(response, "code", -1) != 0 or not getattr(response, "file", None):
            if message_id:
                fallback_request = (
                    GetMessageResourceRequest.builder()
                    .message_id(message_id)
                    .file_key(file_key)
                    .type(resource_type)
                    .build()
                )
                fallback_response = self._client.im.v1.message_resource.get(fallback_request)
                if getattr(fallback_response, "code", -1) == 0 and getattr(fallback_response, "file", None):
                    file_obj = fallback_response.file
                    file_obj.seek(0)
                    return file_obj.read(), getattr(fallback_response, "file_name", None)

            logger.error("Failed to get file {}: code={} msg={}",
                         file_key, getattr(response, "code", ""), getattr(response, "msg", ""))
            return None

        file_obj = response.file
        file_obj.seek(0)
        return file_obj.read(), getattr(response, "file_name", None)


def _parse_message_content(message_type: str, content_raw: str) -> tuple[str, dict]:
    """Parse Feishu message content for all supported types.

    Returns (text_content, media_info) where media_info contains
    download keys, filenames, etc. for non-text types.
    """
    content = ""
    media_info: dict = {}

    try:
        data = json.loads(content_raw) if content_raw else {}
    except (json.JSONDecodeError, TypeError):
        return content_raw or "", media_info

    if message_type == "text":
        content = data.get("text", "")

    elif message_type == "image":
        img_key = data.get("image_key", "")
        content = f"[图片]"
        media_info = {"type": "image", "image_key": img_key}

    elif message_type == "file":
        file_key = data.get("file_key", "")
        file_name = data.get("file_name", "")
        content = f"[文件: {file_name}]" if file_name else "[文件]"
        media_info = {"type": "file", "file_key": file_key, "file_name": file_name}

    elif message_type == "video":
        video_key = data.get("video_key", "")
        file_name = data.get("file_name", "")
        content = f"[视频: {file_name}]" if file_name else "[视频]"
        media_info = {"type": "video", "video_key": video_key, "file_name": file_name}

    elif message_type == "audio":
        audio_key = data.get("file_key", "")
        file_name = data.get("file_name", "")
        content = f"[语音: {file_name}]" if file_name else "[语音]"
        media_info = {"type": "audio", "file_key": audio_key, "file_name": file_name}

    elif message_type == "post":
        # Rich text post — extract plain text from all content sections
        post_payload = data
        if not isinstance(post_payload.get("content"), list):
            for value in post_payload.values():
                if isinstance(value, dict) and isinstance(value.get("content"), list):
                    post_payload = value
                    break

        title = post_payload.get("title", "")
        paragraphs = []
        image_keys: list[str] = []
        for lang_content in post_payload.get("content", []):
            if not isinstance(lang_content, list):
                continue
            for block in lang_content:
                if not isinstance(block, list):
                    # Single element (dict) directly in the content array
                    if isinstance(block, dict):
                        if block.get("tag") == "text":
                            paragraphs.append(block.get("text", ""))
                        elif block.get("tag") == "at":
                            paragraphs.append(block.get("user_name", "@user"))
                        elif block.get("tag") == "a":
                            paragraphs.append(block.get("text", "") or block.get("href", ""))
                        elif block.get("tag") == "img":
                            image_key = block.get("image_key", "")
                            if image_key:
                                image_keys.append(image_key)
                    continue
                for elem in block:
                    if not isinstance(elem, dict):
                        continue
                    if elem.get("tag") == "text":
                        paragraphs.append(elem.get("text", ""))
                    elif elem.get("tag") == "at":
                        paragraphs.append(elem.get("user_name", "@user"))
                    elif elem.get("tag") == "a":
                        paragraphs.append(elem.get("text", "") or elem.get("href", ""))
                    elif elem.get("tag") == "img":
                        image_key = elem.get("image_key", "")
                        if image_key:
                            image_keys.append(image_key)
        body_text = "\n".join(paragraphs)
        if image_keys and not body_text:
            body_text = "[图片]"
        content = f"{title}\n{body_text}" if title and body_text else (title or body_text)
        media_info = {"type": "post", "title": title, "image_keys": image_keys, "has_image": bool(image_keys)}

    elif message_type == "interactive":
        # Interactive card — extract card elements text
        card = data.get("card", {})
        elements = card.get("elements", [])
        texts = []
        header = card.get("header", {})
        if header.get("title", {}).get("tag") == "plain_text":
            texts.append(header["title"].get("content", ""))
        for elem in elements:
            if elem.get("tag") == "div" and elem.get("text", {}).get("tag") == "plain_text":
                texts.append(elem["text"].get("content", ""))
        content = "\n".join(texts) if texts else "[卡片消息]"
        media_info = {"type": "interactive"}

    elif message_type == "sticker":
        # Sticker / emoji
        content = f"[表情]"
        media_info = {"type": "sticker"}

    elif message_type == "share_chat" or message_type == "share_user":
        content = f"[分享]"
        media_info = {"type": message_type}

    else:
        # Unknown type — use raw content as fallback
        content = f"[{message_type}消息] " + (data.get("text", "") or "")
        media_info = {"type": message_type}

    return content, media_info
