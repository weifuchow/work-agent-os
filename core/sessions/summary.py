"""Session summary — generate and update summaries for work sessions."""

import json
from pathlib import Path

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from models.db import Message, Session, SessionMessage

# Threshold: generate summary when message count reaches this
SUMMARY_THRESHOLD = 5


async def update_summary(db: AsyncSession, session: Session) -> None:
    """Generate or update the session summary file.

    Called after a new message is routed to the session.
    Only invokes Claude API when message_count >= SUMMARY_THRESHOLD.
    """
    if session.message_count < SUMMARY_THRESHOLD:
        return

    # Fetch recent messages for this session
    stmt = (
        select(SessionMessage)
        .where(SessionMessage.session_id == session.id)
        .order_by(SessionMessage.sequence_no.desc())
        .limit(10)
    )
    session_msgs = (await db.execute(stmt)).scalars().all()

    if not session_msgs:
        return

    # Load message contents
    messages_text = []
    for sm in reversed(session_msgs):
        msg = await db.get(Message, sm.message_id)
        if msg:
            sender = msg.sender_name or msg.sender_id
            messages_text.append(f"[{sender}] {msg.content}")

    if not messages_text:
        return

    # Generate summary via Claude
    summary_content = await _generate_summary(session, messages_text)

    # Write summary file
    summary_dir = Path(settings.sessions_dir) / str(session.id)
    summary_dir.mkdir(parents=True, exist_ok=True)
    summary_path = summary_dir / "summary.md"
    summary_path.write_text(summary_content, encoding="utf-8")

    session.summary_path = str(summary_path)
    await db.commit()

    logger.info("Summary updated for session {}: {}", session.id, summary_path)


async def get_summary(session: Session) -> str | None:
    """Read the current summary for a session, if it exists."""
    if not session.summary_path:
        return None
    p = Path(session.summary_path)
    if not p.exists():
        return None
    return p.read_text(encoding="utf-8")


async def _generate_summary(session: Session, messages: list[str]) -> str:
    """Call Claude API to generate a structured session summary."""
    from core.orchestrator.claude_client import claude_client

    conversation = "\n".join(messages)

    system_prompt = """你是工作会话摘要生成器。根据会话消息生成结构化摘要。

输出格式（Markdown）：
# 会话摘要

## 问题/主题
（这个会话在讨论什么）

## 已知事实
- （从消息中提取的关键事实）

## 当前结论
- （已达成的共识或决定）

## 未解决点
- （还需要确认或处理的事项）

## 建议动作
- （下一步应该做什么）

保持简洁，每个部分 1-3 条。"""

    prompt = f"会话标题: {session.title}\n项目: {session.project}\n主题: {session.topic}\n\n消息记录:\n{conversation}"

    try:
        text = await claude_client.chat(
            messages=[{"role": "user", "content": prompt}],
            system=system_prompt,
            max_tokens=1024,
        )
        return text.strip()
    except Exception as e:
        logger.warning("Summary generation failed for session {}: {}", session.id, e)
        return f"# 会话摘要\n\n（自动生成失败: {e}）\n\n## 最近消息\n" + "\n".join(f"- {m}" for m in messages[-5:])
