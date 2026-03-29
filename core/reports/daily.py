"""Daily report generator — aggregates from DB, LLM only for final copywriting.

Flow:
1. Query DB for today's stats (NO LLM)
2. Query DB for sessions/messages/actions (NO LLM)
3. Read session summaries from files (NO LLM)
4. LLM generates polished report text (one call)
5. Save to file + push to Feishu
"""

from datetime import datetime, timedelta
from pathlib import Path

from loguru import logger
from sqlalchemy import select, func, and_

from core.config import settings
from core.database import async_session_factory
from models.db import (
    AgentRun, AuditLog, Message, PipelineStatus,
    Report, ReportStatus, Session, SessionStatus,
)


async def generate_daily_report(
    target_date: str | None = None,
    push_to_feishu: bool = True,
    push_chat_id: str = "",
) -> str:
    """Generate daily report from actual processed data.

    Args:
        target_date: YYYY-MM-DD, defaults to yesterday
        push_to_feishu: whether to send via Feishu
        push_chat_id: Feishu chat_id to send to

    Returns:
        Report content (markdown)
    """
    if not target_date:
        yesterday = datetime.now() - timedelta(days=1)
        target_date = yesterday.strftime("%Y-%m-%d")

    # ── Step 1: Gather facts from DB (NO LLM) ──
    facts = await _gather_daily_facts(target_date)

    if facts["total_messages"] == 0:
        report_text = f"# 日报 {target_date}\n\n今日无新消息。"
    else:
        # ── Step 2: Read session summaries (NO LLM) ──
        session_summaries = await _read_session_summaries(facts["active_session_ids"])

        # ── Step 3: LLM generates polished report (one call) ──
        report_text = await _generate_report_text(target_date, facts, session_summaries)

    # ── Step 4: Save to file ──
    report_path = await _save_report(target_date, report_text)

    # ── Step 5: Record in DB ──
    async with async_session_factory() as db:
        report = Report(
            report_date=target_date,
            report_type="daily",
            content_path=str(report_path),
            status=ReportStatus.generated,
        )
        db.add(report)
        await db.commit()
        await db.refresh(report)

        # ── Step 6: Push to Feishu ──
        if push_to_feishu and push_chat_id:
            try:
                from core.connectors.feishu import FeishuClient
                client = FeishuClient()
                # Truncate for Feishu message limit
                content = report_text[:3000]
                client.send_message(receive_id=push_chat_id, content=content)
                report.status = ReportStatus.sent
                report.sent_at = datetime.now()
                await db.commit()
                logger.info("Daily report sent to {}", push_chat_id)
            except Exception as e:
                logger.warning("Failed to push daily report: {}", e)
                report.status = ReportStatus.failed
                await db.commit()

    logger.info("Daily report generated for {}: {}", target_date, report_path)
    return report_text


async def _gather_daily_facts(target_date: str) -> dict:
    """Query DB for daily statistics. Pure SQL, NO LLM."""
    day_start = datetime.fromisoformat(f"{target_date}T00:00:00+00:00")
    day_end = day_start + timedelta(days=1)

    async with async_session_factory() as db:
        # Message counts by classification
        stmt = select(Message.classified_type, func.count(Message.id)).where(
            and_(Message.created_at >= day_start, Message.created_at < day_end)
        ).group_by(Message.classified_type)
        classification = dict((await db.execute(stmt)).all())

        total_messages = sum(classification.values())

        # Pipeline status breakdown
        stmt = select(Message.pipeline_status, func.count(Message.id)).where(
            and_(Message.created_at >= day_start, Message.created_at < day_end)
        ).group_by(Message.pipeline_status)
        pipeline = dict((await db.execute(stmt)).all())

        # Active sessions
        stmt = select(Session).where(
            and_(Session.last_active_at >= day_start, Session.last_active_at < day_end)
        ).order_by(Session.message_count.desc())
        active_sessions = (await db.execute(stmt)).scalars().all()

        # Agent runs
        stmt = select(AgentRun.agent_name, func.count(AgentRun.id)).where(
            and_(AgentRun.started_at >= day_start, AgentRun.started_at < day_end)
        ).group_by(AgentRun.agent_name)
        agent_runs = dict((await db.execute(stmt)).all())

        # New sessions created today
        stmt = select(func.count(Session.id)).where(
            and_(Session.created_at >= day_start, Session.created_at < day_end)
        )
        new_sessions = (await db.execute(stmt)).scalar() or 0

        # Token usage
        stmt = select(
            func.sum(AgentRun.input_tokens),
            func.sum(AgentRun.output_tokens),
        ).where(and_(AgentRun.started_at >= day_start, AgentRun.started_at < day_end))
        token_row = (await db.execute(stmt)).one()
        total_input_tokens = token_row[0] or 0
        total_output_tokens = token_row[1] or 0

        # Conversations — user messages with their bot replies
        user_msgs_stmt = (
            select(Message)
            .where(and_(
                Message.created_at >= day_start,
                Message.created_at < day_end,
                Message.sender_id != "bot",
            ))
            .order_by(Message.created_at)
            .limit(50)
        )
        user_msgs = (await db.execute(user_msgs_stmt)).scalars().all()

        conversations = []
        for um in user_msgs:
            reply_stmt = select(Message).where(
                Message.platform_message_id == f"reply_{um.platform_message_id}"
            )
            reply = (await db.execute(reply_stmt)).scalar_one_or_none()
            conversations.append({
                "sender": um.sender_name or um.sender_id,
                "question": um.content[:200],
                "classified_type": um.classified_type,
                "answer": reply.content[:200] if reply else "(未回复)",
            })

        return {
            "total_messages": total_messages,
            "classification": {(k or "unclassified"): v for k, v in classification.items()},
            "pipeline": {(k or "pending"): v for k, v in pipeline.items()},
            "active_sessions": [
                {"id": s.id, "title": s.title, "topic": s.topic, "project": s.project,
                 "status": s.status, "message_count": s.message_count, "priority": s.priority}
                for s in active_sessions
            ],
            "active_session_ids": [s.id for s in active_sessions],
            "new_sessions": new_sessions,
            "agent_runs": agent_runs,
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "conversations": conversations,
        }


async def _read_session_summaries(session_ids: list[int]) -> dict[int, str]:
    """Read summary files for sessions. File I/O only, NO LLM."""
    summaries = {}
    for sid in session_ids[:10]:  # cap at 10
        summary_path = Path(settings.sessions_dir) / str(sid) / "summary.md"
        if summary_path.exists():
            summaries[sid] = summary_path.read_text(encoding="utf-8")[:500]
    return summaries


async def _generate_report_text(target_date: str, facts: dict, summaries: dict) -> str:
    """LLM generates polished report from pre-gathered facts. Single LLM call."""
    from core.orchestrator.claude_client import claude_client

    system_prompt = """你是日报生成器。根据提供的事实数据生成简洁的工作日报。

格式：
# 工作日报 {日期}

## 概览
（关键数字：消息数、会话数、token 消耗）

## 今日对话
（列出主要问答，标注分类）

## 工作会话
（每个会话一段简述）

## 资源消耗
（token 用量统计）

## 明日关注
（基于未解决事项的建议）

要求：简洁、准确、只基于提供的数据，不要编造。"""

    # Build factual context
    session_lines = []
    for s in facts["active_sessions"][:10]:
        summary = summaries.get(s["id"], "")
        session_lines.append(
            f"- [{s['title']}] 项目={s['project']} 状态={s['status']} "
            f"消息数={s['message_count']} 优先级={s['priority']}\n  摘要: {summary[:200]}"
        )

    conv_lines = []
    for c in facts.get("conversations", [])[:20]:
        conv_lines.append(
            f"- [{c['classified_type']}] {c['sender']}: {c['question'][:80]}\n"
            f"  → {c['answer'][:80]}"
        )

    context = f"""日期: {target_date}

消息统计:
- 总消息: {facts['total_messages']}
- 分类: {facts['classification']}
- 管线状态: {facts['pipeline']}

会话统计:
- 新建会话: {facts['new_sessions']}
- 活跃会话: {len(facts['active_sessions'])}

Token 消耗:
- 输入: {facts.get('total_input_tokens', 0):,}
- 输出: {facts.get('total_output_tokens', 0):,}
- 合计: {facts.get('total_input_tokens', 0) + facts.get('total_output_tokens', 0):,}

Agent 调用: {facts['agent_runs']}

今日对话记录:
{chr(10).join(conv_lines) if conv_lines else '(无)'}

活跃会话详情:
{chr(10).join(session_lines) if session_lines else '(无)'}"""

    try:
        return await claude_client.chat(
            messages=[{"role": "user", "content": context}],
            system=system_prompt,
            max_tokens=2048,
        )
    except Exception as e:
        logger.warning("Report generation LLM failed: {}", e)
        # Fallback: plain text report from facts
        return f"""# 工作日报 {target_date}

## 概览
- 消息: {facts['total_messages']}
- 新建会话: {facts['new_sessions']}
- 活跃会话: {len(facts['active_sessions'])}
- 分类: {facts['classification']}
- Agent 调用: {facts['agent_runs']}

## 活跃会话
{chr(10).join(session_lines) if session_lines else '(无)'}
"""


async def _save_report(target_date: str, content: str) -> Path:
    """Save report to file."""
    report_dir = Path(settings.reports_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"daily_{target_date}.md"
    path.write_text(content, encoding="utf-8")
    return path
