"""Message persistence service — save incoming messages to DB with dedup."""

import json
from datetime import datetime

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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
            from core.config import set_model_override, get_model_override, load_models_config
            old_model = get_model_override() or load_models_config().get("default", "unknown")
            set_model_override(model_id)

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
                client.send_message(chat_id, f"模型已切换: {old_model} → {model_id}")

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
