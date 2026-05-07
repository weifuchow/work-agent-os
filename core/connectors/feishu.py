"""Feishu WebSocket long-connection connector using lark-oapi SDK."""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import uuid
import time
from typing import Any, Callable, Optional

import lark_oapi as lark
from loguru import logger

from core.config import settings
from core.ports import DeliveryResult, DownloadedFile, ReplyPayload


_FEISHU_CARD_ROOT_KEYS = {
    "schema",
    "config",
    "header",
    "body",
    "elements",
    "i18n_header",
    "i18n_elements",
    "card_link",
}


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
        reconnect_delay = max(1, int(os.getenv("FEISHU_WS_RECONNECT_DELAY_SECONDS", "3")))
        logger.info("Starting Feishu WebSocket connection...")

        while True:
            try:
                event_handler = self._build_ws_event_handler()
                ws_client = self._build_ws_client(event_handler)
                ws_client.start()
                logger.warning(
                    "Feishu WebSocket connection exited, reconnecting in {}s",
                    reconnect_delay,
                )
            except Exception as e:
                logger.exception(
                    "Feishu WebSocket connection failed, reconnecting in {}s: {}",
                    reconnect_delay,
                    e,
                )
            self._sleep_for_ws_reconnect(reconnect_delay)

    def _build_ws_event_handler(self):
        return lark.EventDispatcherHandler.builder(
            settings.feishu_verification_token,
            settings.feishu_encrypt_key,
        ).register_p2_im_message_receive_v1(self._handle_message_event).build()

    def _build_ws_client(self, event_handler):
        from lark_oapi.ws import Client as WsClient

        return WsClient(
            settings.feishu_app_id,
            settings.feishu_app_secret,
            event_handler=event_handler,
            log_level=lark.LogLevel.DEBUG if settings.debug else lark.LogLevel.INFO,
        )

    def _sleep_for_ws_reconnect(self, delay_seconds: int) -> None:
        time.sleep(delay_seconds)

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


class FeishuChannelPort:
    """Deliver already-rendered reply payloads to Feishu."""

    def __init__(self, client: FeishuClient | None = None) -> None:
        self.client = client or FeishuClient()

    async def deliver_reply(
        self,
        *,
        source_message: dict,
        reply: ReplyPayload,
    ) -> DeliveryResult:
        try:
            msg_type, content = _feishu_reply_body(reply, client=self.client)
            result = self.client.reply_message(
                message_id=str(source_message.get("platform_message_id") or ""),
                content=content,
                msg_type=msg_type,
                reply_in_thread=True,
            )
        except Exception as exc:
            return DeliveryResult(delivered=False, error=str(exc))

        if not result:
            return DeliveryResult(delivered=False, error="feishu reply returned no result")
        return DeliveryResult(
            delivered=True,
            message_id=str(result.get("message_id") or ""),
            thread_id=str(result.get("thread_id") or ""),
            root_id=str(result.get("root_id") or ""),
            raw=dict(result),
        )


class FeishuFilePort:
    """Download message media resources through Feishu APIs."""

    def __init__(self, client: FeishuClient | None = None) -> None:
        self.client = client or FeishuClient()

    async def download_message_media(
        self,
        *,
        source_message: dict,
        media_info: dict,
    ) -> DownloadedFile | None:
        media_type = str(media_info.get("type") or media_info.get("kind") or "").strip()
        resource_key = (
            media_info.get("image_key")
            or media_info.get("file_key")
            or media_info.get("video_key")
            or media_info.get("resource_id")
            or ""
        )
        if not resource_key:
            return None

        message_id = str(source_message.get("platform_message_id") or "")
        try:
            if media_type == "image":
                payload = self.client.get_image_bytes(
                    str(resource_key),
                    message_id=message_id,
                    resource_type="image",
                )
            else:
                payload = self.client.get_file_bytes(
                    str(resource_key),
                    message_id=message_id,
                    resource_type=media_type or "file",
                )
        except TypeError:
            if media_type == "image":
                payload = self.client.get_image_bytes(str(resource_key))
            else:
                payload = self.client.get_file_bytes(str(resource_key))

        if not payload:
            return None
        data, file_name = payload
        return DownloadedFile(
            data=data,
            file_name=file_name or str(media_info.get("file_name") or ""),
            mime_type=str(media_info.get("mime_type") or media_info.get("mime") or ""),
        )


def _feishu_reply_body(reply: ReplyPayload, client: FeishuClient | None = None) -> tuple[str, str]:
    reply_type = reply.type
    if reply_type == "feishu_card":
        payload = _sanitize_feishu_card_payload(reply.payload)
        if payload:
            payload = _replace_mermaid_blocks_with_images(payload, client=client)
        if not payload:
            fallback = _safe_text_fallback(reply.content)
            return "text", fallback or "回复卡片格式异常，已改用文本兜底。"
        return "interactive", json.dumps(payload, ensure_ascii=False)
    if reply_type == "file":
        return "text", _safe_text_fallback(reply.content or str(reply.file_path or ""))
    return "text", _safe_text_fallback(reply.content)


def _sanitize_feishu_card_payload(payload: Any) -> dict[str, Any]:
    """Keep reply.payload as a Feishu card only.

    Agent outputs may carry machine-readable summaries next to the card. Feishu
    rejects unknown root properties, so channel adapters must strip those before
    delivery.
    """
    if not isinstance(payload, dict):
        return {}

    candidate = payload
    for key in ("card", "payload"):
        nested = candidate.get(key)
        if isinstance(nested, dict) and _looks_like_feishu_card(nested):
            candidate = nested
            break

    if not _looks_like_feishu_card(candidate):
        return {}

    cleaned = {
        key: value
        for key, value in candidate.items()
        if key in _FEISHU_CARD_ROOT_KEYS
    }
    removed = sorted(set(candidate) - set(cleaned))
    if removed:
        logger.warning("Stripped non-Feishu card root fields before delivery: {}", removed)
    return cleaned


_MERMAID_BLOCK_RE = re.compile(r"```mermaid\s*(.*?)```", re.IGNORECASE | re.DOTALL)


class _ElementReplacement:
    def __init__(self, elements: list[dict[str, Any]]) -> None:
        self.elements = elements


def _replace_mermaid_blocks_with_images(
    payload: dict[str, Any],
    *,
    client: FeishuClient | None,
) -> dict[str, Any]:
    rewritten = _rewrite_mermaid_value(payload, client=client)
    if isinstance(rewritten, dict):
        return rewritten
    return payload


def _rewrite_mermaid_value(value: Any, *, client: FeishuClient | None) -> Any:
    if isinstance(value, list):
        rewritten_items: list[Any] = []
        for item in value:
            rewritten = _rewrite_mermaid_value(item, client=client)
            if isinstance(rewritten, _ElementReplacement):
                rewritten_items.extend(rewritten.elements)
            else:
                rewritten_items.append(rewritten)
        return rewritten_items

    if not isinstance(value, dict):
        return value

    tag = str(value.get("tag") or "").strip().lower()
    if tag == "mermaid":
        return _mermaid_element_to_image_or_fallback(value, client=client)

    if tag == "markdown":
        content = str(value.get("content") or "")
        if "```mermaid" in content.lower():
            return _ElementReplacement(
                _markdown_mermaid_blocks_to_elements(value, client=client)
            )

    rewritten_dict: dict[str, Any] = {}
    for key, item in value.items():
        rewritten = _rewrite_mermaid_value(item, client=client)
        rewritten_dict[key] = rewritten.elements if isinstance(rewritten, _ElementReplacement) else rewritten
    return rewritten_dict


def _markdown_mermaid_blocks_to_elements(
    element: dict[str, Any],
    *,
    client: FeishuClient | None,
) -> list[dict[str, Any]]:
    content = str(element.get("content") or "")
    elements: list[dict[str, Any]] = []
    stripped = _MERMAID_BLOCK_RE.sub("", content).strip()
    if stripped:
        item = dict(element)
        item["content"] = stripped
        elements.append(item)

    for index, match in enumerate(_MERMAID_BLOCK_RE.finditer(content), start=1):
        mermaid_source = match.group(1).strip()
        elements.append(
            _mermaid_to_image_or_fallback(
                mermaid_source,
                title=f"流程图 {index}",
                client=client,
            )
        )
    return elements


def _mermaid_element_to_image_or_fallback(
    element: dict[str, Any],
    *,
    client: FeishuClient | None,
) -> dict[str, Any]:
    title = str(element.get("title") or element.get("name") or "流程图").strip()
    source = _extract_mermaid_source(element)
    return _mermaid_to_image_or_fallback(source, title=title, client=client)


def _extract_mermaid_source(element: dict[str, Any]) -> str:
    for key in ("source", "content", "code", "mermaid", "text"):
        value = element.get(key)
        if isinstance(value, str) and value.strip():
            match = _MERMAID_BLOCK_RE.search(value)
            return (match.group(1) if match else value).strip()
    return ""


def _mermaid_to_image_or_fallback(
    mermaid_source: str,
    *,
    title: str,
    client: FeishuClient | None,
) -> dict[str, Any]:
    image_key = _render_and_upload_mermaid(mermaid_source, client=client) if client else ""
    if image_key:
        return {
            "tag": "img",
            "img_key": image_key,
            "alt": {
                "tag": "plain_text",
                "content": title[:80] or "流程图",
            },
            "mode": "fit_horizontal",
        }
    return {
        "tag": "markdown",
        "content": f"**{title or '流程图'}**\n流程图渲染失败，已拦截原始 Mermaid 代码；请查看 session 产物中的 `.md` 流程图文件。",
        "text_align": "left",
        "text_size": "normal",
    }


def _render_and_upload_mermaid(mermaid_source: str, *, client: FeishuClient | None) -> str:
    if not mermaid_source.strip():
        return ""
    if client is None:
        return ""
    image_path = _render_mermaid_png(mermaid_source)
    if not image_path:
        return ""
    try:
        return str(client.upload_image(str(image_path)) or "")
    except Exception as exc:
        logger.warning("Failed to upload rendered Mermaid image {}: {}", image_path, exc)
        return ""


def _render_mermaid_png(mermaid_source: str) -> Path | None:
    output_dir = settings.project_root / ".tmp" / "mermaid"
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = uuid.uuid4().hex

    commands: list[list[str]] = []
    mmdc = shutil.which("mmdc") or shutil.which("mmdc.cmd")
    if mmdc:
        commands.append([mmdc, "-b", "transparent"])
    npx = shutil.which("npx.cmd") or shutil.which("npx")
    if npx:
        commands.append([
            npx,
            "--yes",
            "@mermaid-js/mermaid-cli",
            "-b",
            "transparent",
        ])

    failure_details: list[str] = []
    for source_index, source in enumerate(_mermaid_render_sources(mermaid_source), start=1):
        source_path = output_dir / f"{stem}_{source_index}.mmd"
        output_path = output_dir / f"{stem}_{source_index}.png"
        source_path.write_text(source.strip() + "\n", encoding="utf-8")

        for base_command in commands:
            command = [
                *base_command,
                "-i",
                str(source_path),
                "-o",
                str(output_path),
            ]
            try:
                result = subprocess.run(
                    command,
                    cwd=str(settings.project_root),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=90,
                    check=False,
                )
            except Exception as exc:
                failure_details.append(str(exc))
                continue
            if result.returncode == 0 and output_path.exists() and output_path.stat().st_size > 0:
                return output_path
            detail = (result.stderr or result.stdout or "").strip()
            if detail:
                failure_details.append(detail[:500])
    if failure_details:
        logger.warning("Mermaid render failed: {}", failure_details[-1])
    return None


def _mermaid_render_sources(mermaid_source: str) -> list[str]:
    source = str(mermaid_source or "").strip()
    if not source:
        return []
    normalized = _normalize_mermaid_flowchart_labels(source)
    if normalized != source:
        return [source, normalized]
    return [source]


_MERMAID_NODE_LABEL_RE = re.compile(
    r"(?<!\[)\b([A-Za-z][A-Za-z0-9_]*)\[([^\[\]\r\n]+)\](?!\])"
)


def _normalize_mermaid_flowchart_labels(mermaid_source: str) -> str:
    """Quote plain flowchart labels before rendering.

    Mermaid's parser treats characters such as parentheses and arrows inside an
    unquoted label as syntax. The agent often emits readable labels like
    ``A[actuator存在 -> nextStage(Cancel)]``; converting them to quoted labels
    keeps the diagram renderable without asking the model to repair it.
    """

    def replace(match: re.Match[str]) -> str:
        label = match.group(2).strip()
        if not label or label[0] in {'"', "'", "`", "("}:
            return match.group(0)
        safe_label = label.replace("\\", "/").replace('"', "'")
        return f'{match.group(1)}["{safe_label}"]'

    return _MERMAID_NODE_LABEL_RE.sub(replace, mermaid_source)


def _safe_text_fallback(content: str) -> str:
    text = str(content or "")
    if _looks_like_contract_json_text(text):
        recovered = _extract_json_string_field(text, "content")
        if recovered:
            return recovered
        return "回复卡片格式异常，已拦截原始 JSON。请重试或查看审计日志定位 agent 输出。"
    return text


def _looks_like_contract_json_text(text: str) -> bool:
    prefix = str(text or "").lstrip()[:2000]
    return '"action"' in prefix and '"reply"' in prefix and ("feishu_card" in prefix or '"channel"' in prefix)


def _extract_json_string_field(text: str, field_name: str) -> str:
    pattern = re.compile(rf'"{re.escape(field_name)}"\s*:\s*')
    decoder = json.JSONDecoder()
    for match in pattern.finditer(text):
        try:
            value, _end = decoder.raw_decode(text[match.end():])
        except json.JSONDecodeError:
            continue
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _looks_like_feishu_card(value: dict[str, Any]) -> bool:
    return any(key in value for key in ("schema", "body", "elements", "header"))
