"""Message persistence service — save incoming messages to DB with dedup."""

from datetime import UTC, datetime

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

    msg = Message(
        platform=event_data.get("platform", "feishu"),
        platform_message_id=platform_message_id,
        chat_id=event_data.get("chat_id", ""),
        sender_id=event_data.get("sender_id", ""),
        sender_name=event_data.get("sender_name", ""),
        message_type=event_data.get("message_type", "text"),
        content=event_data.get("content", ""),
        received_at=datetime.now(UTC),
        raw_payload=event_data.get("raw_payload", ""),
    )
    session.add(msg)

    # Audit log
    audit = AuditLog(
        event_type="message_received",
        target_type="message",
        target_id=platform_message_id,
        detail=f"from={event_data.get('sender_id', '')} chat={event_data.get('chat_id', '')}",
        operator="feishu_connector",
    )
    session.add(audit)

    await session.commit()
    await session.refresh(msg)

    logger.info("Message saved: id={}, platform_id={}", msg.id, platform_message_id)
    return msg


async def save_and_process(session: AsyncSession, event_data: dict) -> Message | None:
    """Save message and trigger async pipeline processing."""
    import asyncio
    from core.pipeline import process_message

    msg = await save_message(session, event_data)
    if msg:
        # Schedule pipeline processing as a background task
        asyncio.create_task(process_message(msg.id))
        logger.info("Pipeline scheduled for message {}", msg.id)
    return msg
