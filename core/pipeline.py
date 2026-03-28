"""Message processing pipeline — single orchestrator entry point.

The orchestrator agent receives every incoming message and autonomously
decides how to handle it: reply directly, invoke skills (intake, context,
analysis, reply), route to sessions, link task contexts, etc.

Each run:
1. Loads message from DB (no LLM)
2. Builds prompt with message context
3. Single agent_client.run() — model decides everything
4. Extracts structured result, updates DB
"""

import json
from datetime import UTC, datetime

from loguru import logger

from core.database import async_session_factory
from models.db import (
    AgentRun, AgentRunStatus, AuditLog, Message, PipelineStatus, SessionMessage,
)


# ---------------------------------------------------------------------------
# Orchestrator System Prompt
# ---------------------------------------------------------------------------

ORCHESTRATOR_SYSTEM_PROMPT = """你是用户的私人工作助理，运行在用户的电脑上。你通过飞书接收消息，需要理解消息内容并做出恰当的处理。

## 可用 Skills（子 Agent）

你可以调用以下 skills 来完成复杂任务：

- **intake**: 消息分类 — 当你需要对消息进行结构化分类时使用
- **context**: 上下文检索 — 当你需要获取会话历史、项目知识等背景信息时使用
- **analysis**: 问题分析 — 当你需要对复杂工作问题进行深度分析时使用
- **reply**: 回复生成 — 当你需要生成正式的对外回复并判断风险等级时使用
- **report**: 日报生成 — 当需要生成工作日报时使用

## 可用 Tools

- `query_db`: 查询数据库（消息、会话、任务等）
- `send_feishu_message`: 通过飞书发送消息
- `write_audit_log`: 写入审计日志
- `read_memory`: 读取长期记忆（项目知识、人物画像）
- `write_memory`: 写入长期记忆
- `update_session`: 更新会话状态
- `route_to_session`: 将消息路由到工作会话（查找已有或创建新会话）
- `link_task_context`: 将会话关联到任务上下文（查找已有或创建新任务）

## 处理策略

根据消息内容自主判断处理方式：

### 闲聊 / 简单问候
直接回复，不需要调用 skill。通过 `send_feishu_message` 发送回复。

### 工作问题 / 任务请求
1. 使用 `route_to_session` 将消息路由到工作会话
2. 使用 `link_task_context` 关联任务上下文（先用 `query_db` 查看已有 task_contexts，判断是否匹配）
3. 按需调用 intake → context → analysis → reply 链
4. 根据 reply 的策略决定是否发送

### 紧急问题
1. 路由到会话
2. 跳过深度分析，快速生成回复
3. 标记 risk_level=high

### 噪音（表情包、无意义消息）
记录审计日志，不回复。

## 输出要求

处理完成后，你的最终输出必须是一个 JSON 对象：
```json
{
  "action": "replied | drafted | silent",
  "classified_type": "work_question | urgent_issue | task_request | chat | noise",
  "topic": "消息主题",
  "session_id": null,
  "task_context_id": null,
  "reply_content": "回复内容（如有）",
  "reason": "处理理由"
}
```

## 注意事项
- 每条消息都要写 `write_audit_log` 记录处理过程
- 不要对闲聊消息调用 intake/analysis，直接回复即可
- 高风险事项（承诺排期、确认上线、技术方案拍板）必须 draft，不能 auto
- 回复要简洁、专业、有帮助
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def process_message(message_id: int) -> None:
    """Process a message through the orchestrator agent."""
    async with async_session_factory() as db:
        msg = await db.get(Message, message_id)
        if not msg:
            logger.warning("Pipeline: message {} not found", message_id)
            return
        if msg.pipeline_status == PipelineStatus.completed:
            return

        try:
            # Mark as processing
            msg.pipeline_status = PipelineStatus.classifying
            await db.commit()

            # Build prompt
            prompt = _build_prompt(msg)

            # Record agent run
            run = AgentRun(
                message_id=msg.id,
                agent_name="orchestrator",
                runtime_type="agent_sdk",
                status=AgentRunStatus.running,
                started_at=datetime.now(UTC),
            )
            db.add(run)
            await db.commit()
            await db.refresh(run)

            # Run orchestrator agent
            from core.orchestrator.agent_client import agent_client
            result = await agent_client.run(
                prompt=prompt,
                system_prompt=ORCHESTRATOR_SYSTEM_PROMPT,
                max_turns=30,
            )

            result_text = result.get("text", "")
            agent_session_id = result.get("session_id")

            # Parse structured result from agent output
            parsed = _parse_result(result_text)

            # Update message with classification
            msg.classified_type = parsed.get("classified_type", "chat")
            msg.session_id = parsed.get("session_id") or msg.session_id
            msg.pipeline_status = PipelineStatus.completed
            msg.processed_at = datetime.now(UTC)
            await db.commit()

            # Save bot reply if agent sent one
            reply_content = parsed.get("reply_content", "")
            action = parsed.get("action", "silent")
            if reply_content and action in ("replied", "drafted"):
                await _save_reply(db, msg, reply_content,
                                  session_id=parsed.get("session_id"),
                                  is_draft=(action == "drafted"))

            # Update agent run
            run.status = AgentRunStatus.success
            run.ended_at = datetime.now(UTC)
            run.output_path = result_text[:2000]
            run.session_id = parsed.get("session_id")
            if agent_session_id:
                run.input_path = f"agent_session:{agent_session_id}"
            await db.commit()

            # Audit
            db.add(AuditLog(
                event_type="pipeline_completed",
                target_type="message",
                target_id=str(msg.id),
                detail=f"action={action} type={msg.classified_type} session={msg.session_id}",
                operator="orchestrator",
            ))
            await db.commit()

            logger.info("Pipeline: message {} → {} ({})",
                        msg.id, action, msg.classified_type)

        except Exception as e:
            logger.exception("Pipeline failed for message {}: {}", message_id, e)
            async with async_session_factory() as db2:
                m = await db2.get(Message, message_id)
                if m:
                    m.pipeline_status = PipelineStatus.failed
                    m.pipeline_error = str(e)[:500]
                    m.processed_at = datetime.now(UTC)
                    await db2.commit()
                db2.add(AuditLog(
                    event_type="pipeline_failed",
                    target_type="message",
                    target_id=str(message_id),
                    detail=f"error={str(e)[:200]}",
                    operator="orchestrator",
                ))
                await db2.commit()


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
# Helpers (no LLM)
# ---------------------------------------------------------------------------

def _build_prompt(msg: Message) -> str:
    """Build the orchestrator prompt from a message."""
    return f"""新消息到达，请处理：

- 消息ID: {msg.id}
- 发送者: {msg.sender_name or msg.sender_id} (ID: {msg.sender_id})
- 聊天ID: {msg.chat_id}
- 消息类型: {msg.message_type}
- 时间: {msg.received_at.isoformat() if msg.received_at else 'unknown'}

消息内容:
{msg.content}"""


def _parse_result(text: str) -> dict:
    """Extract the JSON result from the agent's final output."""
    text = text.strip()

    # Try to find JSON in the text
    # First try: the whole text is JSON
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass

    # Second try: find JSON block in markdown
    if "```" in text:
        for block in text.split("```"):
            block = block.strip()
            if block.startswith("json"):
                block = block[4:].strip()
            try:
                return json.loads(block)
            except (json.JSONDecodeError, ValueError):
                continue

    # Third try: find last {...} in text
    start = text.rfind("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except (json.JSONDecodeError, ValueError):
            pass

    # Fallback
    return {
        "action": "silent",
        "classified_type": "chat",
        "topic": "",
        "reply_content": text if len(text) < 500 else "",
        "reason": "failed to parse structured output",
    }


async def _save_reply(db, original_msg: Message, reply_content: str,
                      session_id: int | None = None, is_draft: bool = False) -> Message:
    """Save bot reply to messages table and link to session."""
    from sqlalchemy import func, select

    prefix = "[草稿] " if is_draft else ""
    reply_msg = Message(
        platform=original_msg.platform,
        platform_message_id=f"reply_{original_msg.platform_message_id}",
        chat_id=original_msg.chat_id,
        sender_id="bot",
        sender_name="WorkAgent",
        message_type="text",
        content=f"{prefix}{reply_content}",
        received_at=datetime.now(UTC),
        classified_type="bot_reply",
        session_id=session_id,
        pipeline_status=PipelineStatus.completed,
        processed_at=datetime.now(UTC),
    )
    db.add(reply_msg)
    await db.commit()
    await db.refresh(reply_msg)

    if session_id:
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

    return reply_msg
