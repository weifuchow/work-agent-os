"""Session Router — assign messages to work sessions."""

from datetime import datetime, timedelta

from loguru import logger
from sqlalchemy import select, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession

from models.db import Message, Session, SessionMessage, SessionStatus


# How far back to consider a session "recently active"
ACTIVE_WINDOW = timedelta(hours=2)


async def route_message(db: AsyncSession, msg: Message, intake_result: dict) -> Session:
    """Route a classified work message to an existing or new session.

    Matching strategy (simple rules first):
    1. Same chat_id + same project + active within 2h → attach
    2. Same chat_id + topic keyword overlap + active within 2h → attach
    3. No match → create new session
    """
    topic = intake_result.get("topic", "")
    project = intake_result.get("project", "")
    priority = intake_result.get("priority", "normal")
    cutoff = datetime.now() - ACTIVE_WINDOW

    # Strategy 1: exact chat_id + project match within active window
    if project:
        session = await _find_session(db, msg.chat_id, project=project, cutoff=cutoff)
        if session:
            await _attach_message(db, session, msg)
            logger.info("Router: message {} → existing session {} (chat+project match)",
                        msg.id, session.id)
            return session

    # Strategy 2: same chat_id + active window (same conversation thread)
    session = await _find_session(db, msg.chat_id, cutoff=cutoff)
    if session:
        # Update project/topic if new info available
        if project and not session.project:
            session.project = project
        if topic and not session.topic:
            session.topic = topic
        await _attach_message(db, session, msg)
        logger.info("Router: message {} → existing session {} (chat_id match)",
                     msg.id, session.id)
        return session

    # Strategy 3: create new session
    session = await _create_session(db, msg, intake_result)
    await _attach_message(db, session, msg)
    logger.info("Router: message {} → new session {} ({})",
                msg.id, session.id, session.session_key)
    return session


async def _find_session(
    db: AsyncSession,
    chat_id: str,
    project: str | None = None,
    cutoff: datetime | None = None,
) -> Session | None:
    """Find a matching open session."""
    conditions = [
        Session.source_chat_id == chat_id,
        Session.status.in_([SessionStatus.open, SessionStatus.waiting]),
    ]
    if project:
        conditions.append(Session.project == project)
    if cutoff:
        conditions.append(Session.last_active_at >= cutoff)

    stmt = (
        select(Session)
        .where(and_(*conditions))
        .order_by(Session.last_active_at.desc())
        .limit(1)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def _create_session(db: AsyncSession, msg: Message, intake_result: dict) -> Session:
    """Create a new work session from the message + classification result."""
    now = datetime.now()
    timestamp = now.strftime("%Y%m%d_%H%M%S")
    session_key = f"{msg.platform}_{msg.chat_id[:16]}_{timestamp}"

    session = Session(
        session_key=session_key,
        source_platform=msg.platform,
        source_chat_id=msg.chat_id,
        owner_user_id=msg.sender_id,
        title=intake_result.get("summary", "")[:128] or intake_result.get("topic", "")[:128],
        topic=intake_result.get("topic", ""),
        project=intake_result.get("project", ""),
        priority=intake_result.get("priority", "normal"),
        status=SessionStatus.open,
        last_active_at=now,
        message_count=0,
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return session


async def _attach_message(db: AsyncSession, session: Session, msg: Message) -> None:
    """Attach a message to a session and update session metadata."""
    session.message_count += 1
    session.last_active_at = datetime.now()
    session.updated_at = datetime.now()

    sm = SessionMessage(
        session_id=session.id,
        message_id=msg.id,
        role="user",
        sequence_no=session.message_count,
    )
    db.add(sm)
    await db.commit()
