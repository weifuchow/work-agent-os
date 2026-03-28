"""Message processing pipeline — classify → route → summarize → analyze → reply.

Each stage:
1. Updates DB status (queryable without LLM)
2. Writes audit log (queryable without LLM)
3. Sends Feishu progress notification (no LLM needed)
"""

import json
from datetime import UTC, datetime

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import async_session_factory
from models.db import AgentRun, AgentRunStatus, AuditLog, Message, PipelineStatus, SessionMessage


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def process_message(message_id: int) -> None:
    """Full pipeline: classify → route → summarize → analyze → reply."""
    async with async_session_factory() as db:
        msg = await db.get(Message, message_id)
        if not msg:
            logger.warning("Pipeline: message {} not found", message_id)
            return
        if msg.pipeline_status == PipelineStatus.completed:
            return

        try:
            # ── Step 1: Classify ──
            await _set_status(db, msg, PipelineStatus.classifying)
            intake_result = await _classify_message(msg)
            msg.classified_type = intake_result.get("classified_type", "chat")
            await db.commit()
            await _audit(db, "message_classified", "message", msg.id,
                         f"type={msg.classified_type} topic={intake_result.get('topic','')}")

            # Noise/chat → direct reply via Agent SDK, skip routing
            if msg.classified_type in ("noise", "chat"):
                reply_text = await _quick_chat_reply(msg)
                if reply_text:
                    await _feishu_reply(msg, reply_text)
                    await _save_reply(db, msg, reply_text, session_id=None)
                msg.pipeline_status = PipelineStatus.skipped
                msg.processed_at = datetime.now(UTC)
                await db.commit()
                await _audit(db, "pipeline_skipped", "message", msg.id,
                             f"type={msg.classified_type} replied={bool(reply_text)}")
                return

            # ── Step 2: Route ──
            await _set_status(db, msg, PipelineStatus.routing)
            from core.sessions.router import route_message
            session = await route_message(db, msg, intake_result)
            msg.session_id = session.id
            await db.commit()
            await _audit(db, "message_routed", "message", msg.id,
                         f"session={session.id}")

            # ── Step 3: Ack via Feishu (no LLM) ──
            await _feishu_notify(msg, f"收到，正在处理「{intake_result.get('topic', msg.content[:30])}」...")

            # ── Step 4: Summary ──
            from core.sessions.summary import update_summary
            await update_summary(db, session)

            # ── Step 5: Analyze ──
            run = await _create_agent_run(db, session.id, "analysis")
            analysis_text = await _run_analysis(db, msg, session, intake_result)
            await _finish_agent_run(db, run, analysis_text)
            await _feishu_notify(msg, "分析完成，生成回复中...")

            # ── Step 6: Reply ──
            run = await _create_agent_run(db, session.id, "reply")
            reply_result = await _run_reply(db, msg, session, analysis_text)
            await _finish_agent_run(db, run, json.dumps(reply_result, ensure_ascii=False))

            # Execute reply strategy
            strategy = reply_result.get("strategy", "silent")
            reply_content = reply_result.get("reply_content", "")

            if strategy == "auto" and reply_content:
                await _feishu_reply(msg, reply_content)
                await _save_reply(db, msg, reply_content, session_id=session.id)
                await _audit(db, "reply_sent", "message", msg.id,
                             f"strategy=auto len={len(reply_content)}")
            elif strategy == "draft" and reply_content:
                await _feishu_notify(msg, f"[草稿待确认]\n{reply_content}")
                await _save_reply(db, msg, f"[草稿] {reply_content}", session_id=session.id)
                await _audit(db, "reply_draft", "message", msg.id,
                             f"strategy=draft len={len(reply_content)}")
            else:
                await _audit(db, "reply_silent", "message", msg.id,
                             f"strategy={strategy} reason={reply_result.get('reason','')}")

            # ── Done ──
            msg.pipeline_status = PipelineStatus.completed
            msg.processed_at = datetime.now(UTC)
            await db.commit()
            await _audit(db, "pipeline_completed", "message", msg.id,
                         f"session={session.id} strategy={strategy}")
            logger.info("Pipeline: message {} → session {} → {}",
                        msg.id, session.id, strategy)

        except Exception as e:
            logger.exception("Pipeline failed for message {}: {}", message_id, e)
            async with async_session_factory() as db2:
                m = await db2.get(Message, message_id)
                if m:
                    m.pipeline_status = PipelineStatus.failed
                    m.pipeline_error = str(e)[:500]
                    m.processed_at = datetime.now(UTC)
                    await db2.commit()
                await _audit(db2, "pipeline_failed", "message", message_id,
                             f"error={str(e)[:200]}")


async def reprocess_message(message_id: int) -> None:
    """Reset and reprocess a message through the pipeline."""
    async with async_session_factory() as db:
        msg = await db.get(Message, message_id)
        if not msg:
            return
        msg.pipeline_status = PipelineStatus.pending
        msg.pipeline_error = ""
        msg.classified_type = None
        msg.session_id = None
        msg.processed_at = None
        await db.commit()
    await process_message(message_id)


# ---------------------------------------------------------------------------
# Classify (LLM)
# ---------------------------------------------------------------------------

async def _classify_message(msg: Message) -> dict:
    from core.orchestrator.claude_client import claude_client

    system_prompt = """你是 Intake Agent，负责对消息进行分类。输出严格 JSON，不要多余文字。

分类标准：
- work_question: 项目进度、技术问题、方案咨询
- urgent_issue: 线上报错、服务宕机、紧急 Bug
- task_request: 明确的任务委托
- chat: 日常闲聊
- noise: 表情包、无意义消息

输出格式：
{"classified_type":"...","topic":"...","project":"...","priority":"high|normal|low","urgency":false,"summary":"..."}"""

    prompt = f"发送者: {msg.sender_name or msg.sender_id}\n聊天: {msg.chat_id}\n内容: {msg.content}"
    try:
        text = await claude_client.chat(
            messages=[{"role": "user", "content": prompt}],
            system=system_prompt,
            max_tokens=512,
        )
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        return json.loads(text)
    except Exception as e:
        logger.warning("Classify failed for msg {}: {}", msg.id, e)
        return {"classified_type": "chat", "topic": "", "project": "",
                "priority": "normal", "summary": msg.content[:100]}


# ---------------------------------------------------------------------------
# Quick chat reply — uses Agent SDK (has Bash, Read, etc. tool access)
# ---------------------------------------------------------------------------

async def _quick_chat_reply(msg: Message) -> str | None:
    """Reply via Claude Code Agent SDK — can execute commands, read files, etc."""
    from core.orchestrator.agent_client import agent_client

    if not msg.content or len(msg.content.strip()) < 2:
        return None

    try:
        result = await agent_client.run(
            prompt=msg.content,
            system_prompt="你是用户的私人工作助理，运行在用户的电脑上。你可以使用 Bash、Read 等工具来帮助用户完成任务。回复简洁，直接给结果。",
            max_turns=5,
        )
        return result.get("text", "")
    except Exception as e:
        logger.warning("Agent reply failed for msg {}: {}", msg.id, e)
        return None


# ---------------------------------------------------------------------------
# Analyze (LLM) — builds context from DB, then analyzes
# ---------------------------------------------------------------------------

async def _run_analysis(db: AsyncSession, msg: Message, session, intake_result: dict) -> str:
    from core.orchestrator.agent_client import agent_client
    from core.sessions.summary import get_summary

    # Build context from DB (no LLM for this part)
    summary = await get_summary(session) or ""
    recent_msgs = await _get_recent_messages(db, session.id, limit=10)

    system_prompt = """你是 Analysis Agent，对工作问题进行深度分析。输出结构化 Markdown。

格式：
## 问题摘要
## 背景信息
## 已知事实
## 初步判断
## 建议动作
## 不确定点"""

    context = f"""会话: {session.title}
项目: {session.project}
主题: {session.topic}
分类: {msg.classified_type}
优先级: {intake_result.get('priority', 'normal')}

会话摘要:
{summary}

最近消息:
{recent_msgs}

当前消息:
{msg.content}"""

    try:
        result = await agent_client.run(
            prompt=context,
            system_prompt=system_prompt,
            max_turns=5,
        )
        return result.get("text", f"分析失败: 无输出")
    except Exception as e:
        logger.warning("Analysis failed for msg {}: {}", msg.id, e)
        return f"分析失败: {e}"


# ---------------------------------------------------------------------------
# Reply — uses Agent SDK (can send Feishu messages via tools)
# ---------------------------------------------------------------------------

async def _run_reply(db: AsyncSession, msg: Message, session, analysis: str) -> dict:
    from core.orchestrator.agent_client import agent_client

    system_prompt = """你是 Reply Agent，根据分析结果生成回复。输出严格 JSON，不要多余文字。

策略判断：
- auto: 低风险、信息明确
- draft: 中高风险（承诺排期、确认上线、技术方案拍板）
- silent: 不需要回复

输出：
{"strategy":"auto|draft|silent","reply_content":"...","reason":"...","risk_level":"low|medium|high"}"""

    prompt = f"""原始消息: {msg.content}
发送者: {msg.sender_name or msg.sender_id}
分类: {msg.classified_type}

分析结果:
{analysis}"""

    try:
        result = await agent_client.run(
            prompt=prompt,
            system_prompt=system_prompt,
            max_turns=3,
        )
        text = result.get("text", "").strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        return json.loads(text)
    except Exception as e:
        logger.warning("Reply generation failed for msg {}: {}", msg.id, e)
        return {"strategy": "silent", "reply_content": "", "reason": f"生成失败: {e}", "risk_level": "high"}


# ---------------------------------------------------------------------------
# Feishu notifications (NO LLM — just sends status text)
# ---------------------------------------------------------------------------

async def _feishu_notify(msg: Message, text: str) -> None:
    """Send a progress notification via Feishu. No LLM involved."""
    try:
        from core.connectors.feishu import FeishuClient
        client = FeishuClient()
        client.send_message(receive_id=msg.chat_id, content=text, receive_id_type="chat_id")
    except Exception as e:
        logger.warning("Feishu notify failed: {}", e)


async def _feishu_reply(msg: Message, text: str) -> None:
    """Reply to the original message via Feishu. No LLM involved."""
    try:
        from core.connectors.feishu import FeishuClient
        client = FeishuClient()
        client.reply_message(message_id=msg.platform_message_id, content=text)
    except Exception as e:
        logger.warning("Feishu reply failed: {}", e)


# ---------------------------------------------------------------------------
# DB helpers (no LLM)
# ---------------------------------------------------------------------------

async def _set_status(db: AsyncSession, msg: Message, status: PipelineStatus) -> None:
    msg.pipeline_status = status
    await db.commit()


async def _audit(db: AsyncSession, event_type: str, target_type: str,
                 target_id, detail: str) -> None:
    db.add(AuditLog(
        event_type=event_type, target_type=target_type,
        target_id=str(target_id), detail=detail, operator="pipeline",
    ))
    await db.commit()


async def _create_agent_run(db: AsyncSession, session_id: int, agent_name: str) -> AgentRun:
    run = AgentRun(
        session_id=session_id,
        agent_name=agent_name,
        runtime_type="claude_api",
        status=AgentRunStatus.running,
        started_at=datetime.now(UTC),
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    return run


async def _finish_agent_run(db: AsyncSession, run: AgentRun, output: str) -> None:
    from core.orchestrator.claude_client import claude_client
    run.status = AgentRunStatus.success
    run.ended_at = datetime.now(UTC)
    run.output_path = output[:2000]
    # Record token usage from last Claude API call
    usage = getattr(claude_client, 'last_usage', {})
    run.input_tokens = usage.get("input_tokens", 0)
    run.output_tokens = usage.get("output_tokens", 0)
    await db.commit()


async def _save_reply(db: AsyncSession, original_msg: Message, reply_content: str,
                      session_id: int | None = None) -> Message:
    """Save bot reply to messages table and link to session."""
    reply_msg = Message(
        platform=original_msg.platform,
        platform_message_id=f"reply_{original_msg.platform_message_id}",
        chat_id=original_msg.chat_id,
        sender_id="bot",
        sender_name="WorkAgent",
        message_type="text",
        content=reply_content,
        received_at=datetime.now(UTC),
        classified_type="bot_reply",
        session_id=session_id,
        pipeline_status=PipelineStatus.completed,
        processed_at=datetime.now(UTC),
    )
    db.add(reply_msg)
    await db.commit()
    await db.refresh(reply_msg)

    # Link to session if exists
    if session_id:
        from sqlalchemy import func, select
        max_seq = (await db.execute(
            select(func.coalesce(func.max(SessionMessage.sequence_no), 0))
            .where(SessionMessage.session_id == session_id)
        )).scalar() or 0

        sm = SessionMessage(
            session_id=session_id,
            message_id=reply_msg.id,
            role="assistant",
            sequence_no=max_seq + 1,
        )
        db.add(sm)
        await db.commit()

    logger.info("Reply saved: id={} reply_to={} session={}",
                reply_msg.id, original_msg.id, session_id)
    return reply_msg


async def _get_recent_messages(db: AsyncSession, session_id: int, limit: int = 10) -> str:
    from sqlalchemy import select
    from models.db import SessionMessage
    stmt = (
        select(SessionMessage)
        .where(SessionMessage.session_id == session_id)
        .order_by(SessionMessage.sequence_no.desc())
        .limit(limit)
    )
    sms = (await db.execute(stmt)).scalars().all()
    lines = []
    for sm in reversed(sms):
        m = await db.get(Message, sm.message_id)
        if m:
            lines.append(f"[{m.sender_name or m.sender_id}] {m.content}")
    return "\n".join(lines) if lines else "(无历史消息)"
