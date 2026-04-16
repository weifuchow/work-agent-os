"""Message persistence service — save incoming messages to DB with dedup."""

import json
import mimetypes
from pathlib import Path
from datetime import datetime

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from models.db import AuditLog, Message


async def save_message(session: AsyncSession, event_data: dict) -> Message | None:
    """Parse event_data from connector and persist to messages table.

    Returns the Message if saved, None if duplicate.
    """
    platform_message_id = event_data["platform_message_id"]

    # Idempotency check
    existing = await session.execute(
        select(Message).where(Message.platform_message_id == platform_message_id)
    )
    if existing.scalar_one_or_none():
        logger.debug("Duplicate message ignored: {}", platform_message_id)
        return None

    now = datetime.now()
    msg = Message(
        platform=event_data.get("platform", "feishu"),
        platform_message_id=platform_message_id,
        chat_id=event_data.get("chat_id", ""),
        sender_id=event_data.get("sender_id", ""),
        sender_name=event_data.get("sender_name", ""),
        message_type=event_data.get("message_type", "text"),
        content=event_data.get("content", ""),
        received_at=now,
        raw_payload=event_data.get("raw_payload", ""),
        media_info_json=json.dumps(event_data.get("media_info") or {}, ensure_ascii=False),
        thread_id=event_data.get("thread_id", ""),
        root_id=event_data.get("root_id", ""),
        parent_id=event_data.get("parent_id", ""),
    )
    session.add(msg)

    # Audit log — include message content for traceability
    audit = AuditLog(
        event_type="message_received",
        target_type="message",
        target_id=platform_message_id,
        detail=json.dumps({
            "sender_id": event_data.get("sender_id", ""),
            "sender_name": event_data.get("sender_name", ""),
            "chat_id": event_data.get("chat_id", ""),
            "chat_type": event_data.get("chat_type", ""),
            "message_type": event_data.get("message_type", ""),
            "content": event_data.get("content", "")[:500],
            "is_mentioned": event_data.get("is_mentioned", False),
        }, ensure_ascii=False),
        operator="feishu_connector",
        created_at=now,
    )
    session.add(audit)

    await session.commit()
    await session.refresh(msg)

    logger.info("Message saved: id={}, platform_id={}", msg.id, platform_message_id)
    return msg


def _sanitize_filename(name: str, fallback: str) -> str:
    candidate = (name or "").strip().replace("\\", "_").replace("/", "_")
    candidate = "".join(ch for ch in candidate if ch not in '<>:"|?*').strip(" .")
    return candidate or fallback


def _guess_suffix(file_name: str, media_type: str) -> str:
    suffix = Path(file_name).suffix
    if suffix:
        return suffix
    if media_type == "image":
        return ".png"
    if media_type == "audio":
        return ".audio"
    if media_type == "video":
        return ".video"
    return ".bin"


async def _download_media_if_needed(session: AsyncSession, msg: Message, event_data: dict) -> None:
    media_info = dict(event_data.get("media_info") or {})
    media_type = str(media_info.get("type") or "").strip()
    if media_type not in {"image", "file", "audio", "video"}:
        return

    remote_key = (
        media_info.get("image_key")
        or media_info.get("file_key")
        or media_info.get("video_key")
        or ""
    )
    if not remote_key:
        return

    from core.connectors.feishu import FeishuClient

    client = FeishuClient()
    payload: tuple[bytes, str | None] | None = None
    if media_type == "image":
        payload = client.get_image_bytes(str(remote_key))
    else:
        payload = client.get_file_bytes(str(remote_key))

    if not payload:
        media_info["download_status"] = "failed"
        msg.media_info_json = json.dumps(media_info, ensure_ascii=False)
        session.add(msg)
        await session.commit()
        return

    data, downloaded_name = payload
    day = datetime.now().strftime("%Y%m%d")
    target_dir = settings.attachments_dir / "feishu" / day / msg.platform_message_id
    target_dir.mkdir(parents=True, exist_ok=True)
    original_dir = target_dir / "original"
    original_dir.mkdir(parents=True, exist_ok=True)

    base_name = _sanitize_filename(
        downloaded_name or str(media_info.get("file_name") or ""),
        fallback=f"{media_type}_{remote_key}",
    )
    suffix = _guess_suffix(base_name, media_type)
    if not Path(base_name).suffix:
        base_name = f"{base_name}{suffix}"

    local_path = original_dir / base_name
    local_path.write_bytes(data)

    mime_type = mimetypes.guess_type(local_path.name)[0] or (
        "image/png" if media_type == "image" else "application/octet-stream"
    )

    media_info.update({
        "download_status": "downloaded",
        "local_path": str(local_path.resolve()),
        "size_bytes": len(data),
        "mime_type": mime_type,
        "proxy_url": f"/api/messages/{msg.id}/attachment",
    })

    manifest_path = target_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps({
            "message_id": msg.id,
            "platform_message_id": msg.platform_message_id,
            "message_type": msg.message_type,
            "media_info": media_info,
        }, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    msg.media_info_json = json.dumps(media_info, ensure_ascii=False)
    msg.attachment_path = str(local_path.resolve())
    session.add(msg)
    await session.commit()


async def save_and_process(session: AsyncSession, event_data: dict) -> Message | None:
    """Save message and trigger async pipeline processing.

    Intercepts special commands (e.g. /m <model>) before pipeline.
    Adds an emoji reaction to acknowledge receipt.
    """
    import asyncio

    content = (event_data.get("content") or "").strip()

    # ── Immediate ack: react 👀 to show "received" ──
    platform_message_id = event_data.get("platform_message_id", "")
    if platform_message_id:
        try:
            from core.connectors.feishu import FeishuClient
            FeishuClient().react_to_message(platform_message_id, emoji="GLANCE")
        except Exception:
            pass  # Non-critical — don't block message processing

    # ── Command: /m <model_id> — switch runtime model ──
    if content.startswith("/m "):
        model_id = content[3:].strip()
        if model_id:
            from core.config import (
                get_agent_runtime_override,
                get_default_model_for_runtime,
                get_model_override,
                load_models_config,
                set_model_override,
                settings,
            )
            from core.orchestrator.agent_runtime import DEFAULT_AGENT_RUNTIME, normalize_agent_runtime

            runtime = normalize_agent_runtime(
                get_agent_runtime_override() or settings.default_agent_runtime or DEFAULT_AGENT_RUNTIME
            )
            config = load_models_config()
            old_model = get_model_override(runtime) or get_default_model_for_runtime(config, runtime) or "unknown"
            set_model_override(model_id, runtime=runtime)

            # Save the command message
            msg = await save_message(session, event_data)

            # Write audit log
            audit = AuditLog(
                event_type="model_switch",
                target_type="model",
                target_id=model_id,
                detail=json.dumps({
                    "old_model": old_model,
                    "new_model": model_id,
                    "runtime": runtime,
                    "sender_id": event_data.get("sender_id", ""),
                    "chat_id": event_data.get("chat_id", ""),
                }, ensure_ascii=False),
                operator="command",
            )
            session.add(audit)
            await session.commit()

            # Reply via feishu
            chat_id = event_data.get("chat_id", "")
            if chat_id:
                from core.connectors.feishu import FeishuClient
                client = FeishuClient()
                client.send_message(chat_id, f"模型已切换({runtime}): {old_model} → {model_id}")

            logger.info("Model switched: {} → {} (by {})", old_model, model_id,
                        event_data.get("sender_id", ""))
            return msg

    from core.pipeline import process_message

    msg = await save_message(session, event_data)
    if msg:
        # Schedule pipeline processing as a background task
        asyncio.create_task(process_message(msg.id))
        logger.info("Pipeline scheduled for message {}", msg.id)
    return msg
