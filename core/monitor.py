"""Task progress monitor — polls DB/files for status changes, pushes to Feishu.

NO LLM calls. Pure DB queries + file reads + Feishu notifications.
"""

from datetime import UTC, datetime, timedelta

from loguru import logger
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import async_session_factory
from models.db import AgentRun, AgentRunStatus, AuditLog, Message, PipelineStatus, Session

# Maximum auto-retries before giving up and marking the message failed.
MAX_STUCK_RETRIES = 3


async def check_running_tasks() -> dict:
    """Check all running tasks and send Feishu progress updates.

    This is called periodically by the scheduler. No LLM involved.
    Returns summary of checks performed.
    """
    async with async_session_factory() as db:
        counts = {"running": 0, "stuck": 0, "notified": 0}

        # 1. Check stuck pipelines (classifying/routing for > 5 min)
        cutoff = datetime.now(UTC) - timedelta(minutes=5)
        stmt = select(Message).where(and_(
            Message.pipeline_status.in_([
                PipelineStatus.classifying, PipelineStatus.routing,
            ]),
            Message.received_at < cutoff,
        ))
        stuck_msgs = (await db.execute(stmt)).scalars().all()
        for msg in stuck_msgs:
            counts["stuck"] += 1
            await _notify_stuck(msg)
            logger.warning("Monitor: message {} stuck in {} for >5min",
                           msg.id, msg.pipeline_status)

        # 2. Check running agent_runs (running for > 3 min)
        agent_cutoff = datetime.now(UTC) - timedelta(minutes=3)
        stmt = select(AgentRun).where(and_(
            AgentRun.status == AgentRunStatus.running,
            AgentRun.started_at < agent_cutoff,
        ))
        stuck_runs = (await db.execute(stmt)).scalars().all()
        for run in stuck_runs:
            counts["running"] += 1
            logger.warning("Monitor: agent_run {} ({}) running for >3min",
                           run.id, run.agent_name)

        # 3. Report recently completed tasks (last check interval)
        interval = datetime.now(UTC) - timedelta(minutes=2)
        stmt = select(Message).where(and_(
            Message.pipeline_status == PipelineStatus.completed,
            Message.processed_at >= interval,
        ))
        recent_completed = (await db.execute(stmt)).scalars().all()
        for msg in recent_completed:
            counts["notified"] += 1

        if counts["stuck"] > 0:
            logger.info("Monitor: {} stuck, {} running, {} recently completed",
                        counts["stuck"], counts["running"], counts["notified"])

        return counts


async def get_task_status_text(session_id: int) -> str:
    """Get human-readable status for a session's tasks. No LLM.

    Returns a plain text summary suitable for Feishu push.
    """
    async with async_session_factory() as db:
        session = await db.get(Session, session_id)
        if not session:
            return "会话不存在"

        # Pipeline status of related messages
        from models.db import SessionMessage
        stmt = (
            select(Message)
            .join(SessionMessage, SessionMessage.message_id == Message.id)
            .where(SessionMessage.session_id == session_id)
            .order_by(Message.created_at.desc())
            .limit(5)
        )
        messages = (await db.execute(stmt)).scalars().all()

        # Agent runs for this session
        stmt = (
            select(AgentRun)
            .where(AgentRun.session_id == session_id)
            .order_by(AgentRun.started_at.desc())
            .limit(5)
        )
        runs = (await db.execute(stmt)).scalars().all()

        lines = [f"📋 会话: {session.title or session.session_key}"]
        lines.append(f"状态: {session.status} | 消息数: {session.message_count}")

        if messages:
            lines.append("\n最近消息:")
            for m in messages:
                status_icon = {
                    "completed": "✅", "failed": "❌", "skipped": "⏭️",
                    "classifying": "🔄", "routing": "🔄", "pending": "⏳",
                }.get(m.pipeline_status, "❓")
                lines.append(f"  {status_icon} #{m.id} {m.pipeline_status} - {m.content[:40]}")

        if runs:
            lines.append("\nAgent 执行:")
            for r in runs:
                status_icon = {"running": "🔄", "success": "✅", "failed": "❌", "pending": "⏳"}.get(r.status, "❓")
                duration = ""
                if r.started_at and r.ended_at:
                    d = (r.ended_at - r.started_at).total_seconds()
                    duration = f" ({d:.1f}s)"
                elif r.started_at:
                    d = (datetime.now(UTC) - r.started_at).total_seconds()
                    duration = f" (已运行{d:.0f}s)"
                lines.append(f"  {status_icon} {r.agent_name}{duration}")

        return "\n".join(lines)


async def _notify_stuck(msg: Message) -> None:
    """Notify about a stuck message via Feishu and auto-retry with cap. No LLM."""
    # Parse retry count from pipeline_error field
    retry_count = 0
    if msg.pipeline_error and msg.pipeline_error.startswith("auto-retry"):
        # Format: "auto-retry #N after stuck"
        try:
            retry_count = int(msg.pipeline_error.split("#")[1].split(" ")[0])
        except (IndexError, ValueError):
            retry_count = 1  # at least one prior retry if prefix exists

    if retry_count >= MAX_STUCK_RETRIES:
        # Give up — mark as failed
        try:
            from core.connectors.feishu import FeishuClient
            client = FeishuClient()
            text = f"❌ 消息 #{msg.id} 处理失败（重试 {retry_count} 次后放弃）"
            client.send_message(receive_id=msg.chat_id, content=text)
        except Exception as e:
            logger.warning("Failed to notify stuck message: {}", e)

        async with async_session_factory() as db:
            m = await db.get(Message, msg.id)
            if m:
                m.pipeline_status = PipelineStatus.failed
                m.pipeline_error = f"exceeded max retries ({MAX_STUCK_RETRIES})"
                m.processed_at = datetime.now(UTC)
                await db.commit()
        return

    try:
        from core.connectors.feishu import FeishuClient
        client = FeishuClient()
        text = f"⚠️ 消息 #{msg.id} 处理卡住（{msg.pipeline_status}），正在重试（{retry_count + 1}/{MAX_STUCK_RETRIES}）..."
        client.send_message(receive_id=msg.chat_id, content=text)
    except Exception as e:
        logger.warning("Failed to notify stuck message: {}", e)

    # Auto-retry by resetting to pending
    async with async_session_factory() as db:
        m = await db.get(Message, msg.id)
        if m and m.pipeline_status not in (PipelineStatus.completed, PipelineStatus.skipped):
            m.pipeline_status = PipelineStatus.pending
            m.pipeline_error = f"auto-retry #{retry_count + 1} after stuck"
            await db.commit()

            import asyncio
            from core.pipeline import process_message
            asyncio.create_task(process_message(msg.id))
