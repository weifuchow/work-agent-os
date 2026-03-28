"""Session lifecycle management — auto-freeze and archive stale sessions."""

from datetime import UTC, datetime, timedelta

from loguru import logger
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import async_session_factory
from models.db import AuditLog, Session, SessionStatus

# Sessions inactive for 24h → waiting
WAITING_THRESHOLD = timedelta(hours=24)
# Sessions inactive for 7 days → archived
ARCHIVE_THRESHOLD = timedelta(days=7)


async def run_lifecycle_check() -> dict:
    """Check all open sessions and update their status based on inactivity.

    Returns counts of transitions made.
    """
    async with async_session_factory() as db:
        now = datetime.now(UTC)
        counts = {"to_waiting": 0, "to_archived": 0}

        # Open sessions inactive > 24h → waiting
        waiting_cutoff = now - WAITING_THRESHOLD
        stmt = select(Session).where(and_(
            Session.status == SessionStatus.open,
            Session.last_active_at < waiting_cutoff,
        ))
        stale_open = (await db.execute(stmt)).scalars().all()
        for s in stale_open:
            s.status = SessionStatus.waiting
            s.updated_at = now
            db.add(AuditLog(
                event_type="session_auto_waiting",
                target_type="session",
                target_id=str(s.id),
                detail=f"inactive since {s.last_active_at.isoformat()}",
                operator="lifecycle",
            ))
            counts["to_waiting"] += 1

        # Waiting/paused sessions inactive > 7 days → archived
        archive_cutoff = now - ARCHIVE_THRESHOLD
        stmt = select(Session).where(and_(
            Session.status.in_([SessionStatus.waiting, SessionStatus.paused]),
            Session.last_active_at < archive_cutoff,
        ))
        stale_waiting = (await db.execute(stmt)).scalars().all()
        for s in stale_waiting:
            s.status = SessionStatus.archived
            s.updated_at = now
            db.add(AuditLog(
                event_type="session_auto_archived",
                target_type="session",
                target_id=str(s.id),
                detail=f"inactive since {s.last_active_at.isoformat()}",
                operator="lifecycle",
            ))
            counts["to_archived"] += 1

        await db.commit()

        if counts["to_waiting"] or counts["to_archived"]:
            logger.info("Lifecycle check: {} → waiting, {} → archived",
                        counts["to_waiting"], counts["to_archived"])
        return counts
