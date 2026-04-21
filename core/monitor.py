"""Task progress monitor — polls DB/files for status changes, pushes to Feishu.

NO LLM calls. Pure DB queries + file reads + Feishu notifications.
"""

from datetime import datetime, timedelta

from loguru import logger
from sqlalchemy import select, and_

from core.database import async_session_factory
from models.db import AgentRun, AgentRunStatus, Message, PipelineStatus, Session

# Time to wait before first "thinking" notification.
THINKING_NOTIFY_MINUTES = 5
# Maximum number of "thinking" notifications per message.
MAX_THINKING_NOTIFICATIONS = 3

# Track notification counts per message (in-memory, reset on restart)
_thinking_counts: dict[int, int] = {}


async def check_running_tasks() -> dict:
    """Check all running tasks and send Feishu progress updates.

    This is called periodically by the scheduler. No LLM involved.
    Returns summary of checks performed.
    """
    async with async_session_factory() as db:
        counts = {"running": 0, "stuck": 0, "notified": 0, "inflight": []}

        # 1. Check slow/stuck pipelines — only notify, no auto-retry
        thinking_cutoff = datetime.now() - timedelta(minutes=THINKING_NOTIFY_MINUTES)

        # Messages still in classifying/routing after THINKING_NOTIFY_MINUTES
        stmt = select(Message).where(and_(
            Message.pipeline_status.in_([
                PipelineStatus.classifying, PipelineStatus.routing,
            ]),
            Message.received_at < thinking_cutoff,
        ))
        slow_msgs = (await db.execute(stmt)).scalars().all()
        for msg in slow_msgs:
            count = _thinking_counts.get(msg.id, 0)
            if count < MAX_THINKING_NOTIFICATIONS:
                await _notify_thinking(msg)
                _thinking_counts[msg.id] = count + 1
                counts["notified"] += 1

        # 2. Collect all running agent_runs (observability, no auto-action)
        stmt = select(AgentRun).where(AgentRun.status == AgentRunStatus.running)
        running_runs = (await db.execute(stmt)).scalars().all()
        now = datetime.now()
        for run in running_runs:
            counts["running"] += 1
            elapsed = (now - run.started_at).total_seconds() if run.started_at else 0
            counts["inflight"].append({
                "id": run.id,
                "agent_name": run.agent_name,
                "message_id": run.message_id,
                "session_id": run.session_id,
                "elapsed_seconds": round(elapsed, 1),
            })

        # 3. Report recently completed tasks (last check interval)
        interval = datetime.now() - timedelta(minutes=2)
        stmt = select(Message).where(and_(
            Message.pipeline_status == PipelineStatus.completed,
            Message.processed_at >= interval,
        ))
        recent_completed = (await db.execute(stmt)).scalars().all()
        for msg in recent_completed:
            counts["notified"] += 1
            _thinking_counts.pop(msg.id, None)  # cleanup

        if counts["running"] > 0:
            logger.info("Monitor: {} inflight, {} notified",
                        counts["running"], counts["notified"])

        return counts


async def get_inflight_summary() -> str:
    """Get a human-readable summary of all in-flight agent tasks. No LLM."""
    async with async_session_factory() as db:
        stmt = select(AgentRun).where(AgentRun.status == AgentRunStatus.running)
        runs = (await db.execute(stmt)).scalars().all()

        if not runs:
            return "当前没有正在执行的任务"

        now = datetime.now()
        lines = [f"当前 {len(runs)} 个任务正在执行："]
        for r in runs:
            elapsed = (now - r.started_at).total_seconds() if r.started_at else 0
            if elapsed < 60:
                duration_str = f"{elapsed:.0f}s"
            else:
                duration_str = f"{elapsed / 60:.1f}min"
            msg_info = f" (消息#{r.message_id})" if r.message_id else ""
            lines.append(f"  🔄 [{r.agent_name}] 已运行 {duration_str}{msg_info}")

        return "\n".join(lines)


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
                    d = (datetime.now() - r.started_at).total_seconds()
                    duration = f" (已运行{d:.0f}s)"
                lines.append(f"  {status_icon} {r.agent_name}{duration}")

        return "\n".join(lines)


async def _notify_thinking(msg: Message) -> None:
    """Reply 'thinking' in the thread for slow-but-alive messages. No LLM.

    Sends a progress update with cumulative elapsed time each check cycle.
    Uses reply_message to post inside the thread/topic.
    """
    try:
        from core.connectors.feishu import FeishuClient

        # Re-check status from DB — message may have completed since query
        async with async_session_factory() as db:
            fresh = await db.get(Message, msg.id)
            if not fresh or fresh.pipeline_status not in (
                PipelineStatus.classifying, PipelineStatus.routing,
            ):
                return

        client = FeishuClient()
        elapsed = (datetime.now() - msg.received_at).total_seconds()
        if elapsed < 120:
            duration_str = f"{elapsed:.0f}秒"
        else:
            minutes = int(elapsed // 60)
            duration_str = f"{minutes}分钟"

        text = f"🤔 模型正在思考中，请稍等... 已思考 {duration_str}"

        # Reply in thread if possible (creates thread on first reply)
        if msg.platform_message_id:
            result = client.reply_message(
                message_id=msg.platform_message_id,
                content=text,
                reply_in_thread=True,
            )
            # Bind thread_id to session if new thread created
            if result and msg.session_id:
                thread_id = result.get("thread_id", "")
                if thread_id:
                    try:
                        async with async_session_factory() as db:
                            sess = await db.get(Session, msg.session_id)
                            if sess and not sess.thread_id:
                                sess.thread_id = thread_id
                                await db.commit()
                    except Exception:
                        pass
        else:
            client.send_message(receive_id=msg.chat_id, content=text)
    except Exception as e:
        logger.warning("Failed to send thinking notification: {}", e)
