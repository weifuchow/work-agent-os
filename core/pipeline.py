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
from datetime import datetime

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
- `save_bot_reply`: 保存回复到数据库（发送飞书消息后必须调用，确保回复可追溯）
- `write_audit_log`: 写入审计日志
- `read_memory`: 读取长期记忆（项目知识、人物画像）
- `write_memory`: 写入长期记忆
- `update_session`: 更新会话状态
- `route_to_session`: 将消息路由到工作会话（查找已有或创建新会话）
- `link_task_context`: 将会话关联到任务上下文（查找已有或创建新任务）
- `list_projects`: 列出所有已注册项目（名称 + 描述），用于判断消息涉及哪个项目
- `dispatch_to_project`: 将任务派发到指定项目目录下执行，使用该项目的 skills，返回结果

## 处理策略

**核心规则**：除了"你好"、"谢谢"、"早上好"等纯社交问候语，所有消息都必须先调用 `route_to_session` 创建或匹配会话。这是强制要求，不可跳过。调用 `route_to_session` 后，将返回的 `session_id` 填入最终 JSON 输出。

根据消息内容自主判断处理方式：

### 闲聊 / 简单问候（仅限"你好""早上好""谢谢"等纯社交语）
直接回复，不需要调用 skill。通过 `send_feishu_message` 发送回复，然后调用 `save_bot_reply` 保存记录。

### 所有其他消息（包括工作问题、知识问询、技术讨论、系统查询等）
1. **第一步必须调用** `route_to_session`（传入 chat_id、sender_id、message_id、topic）
2. 使用 `link_task_context` 关联任务上下文
3. 如果需要执行操作获取信息（查磁盘、读文件等），可以使用 Bash/Read/Glob 等工具
4. 通过 `send_feishu_message` 发送回复，然后调用 `save_bot_reply` 保存
5. 输出 JSON 时 `session_id` **必须**填入 `route_to_session` 返回的 session_id
**不要静默**，所有消息都必须有回复。

## 项目路由

当消息涉及具体项目工作（代码修改、Bug 修复、测试、部署等）时：
1. 调用 `list_projects` 获取可用项目列表
2. 根据消息内容、关键词、涉及的技术栈判断属于哪个项目
3. 调用 `dispatch_to_project` 将任务派发到对应项目目录下的 Agent
4. 项目 Agent 在该项目目录下执行，可读取项目文件、运行命令、使用项目特定 skills
5. 将项目 Agent 的结果纳入回复

### 多轮对话（项目 session resume）

当用户就同一个项目连续对话时（比如先分析问题，再确认修复方案）：
1. 首次 dispatch 会返回 `session_id`（Agent SDK session）
2. 用 `route_to_session` 将消息路由到工作会话后，会话中会保存 `agent_session_id`
3. 后续消息 dispatch 同一项目时，传入 `session_id=agent_session_id` 和 `db_session_id`
4. 项目 Agent 会恢复之前的完整上下文（包括它读过的文件、执行过的命令、生成的方案）

这样用户可以通过飞书进行多轮交互：
- "帮我分析 allspark fcs 模块的空指针异常" → 首轮分析
- "用 code-fixer 修一下" → resume 之前的 session，继续在同一上下文中修复
- "方案 B 看起来更好，执行吧" → 继续 resume，确认并执行

如果无法确定项目归属，先在全局范围处理，或回复询问具体项目。

## 输出要求

处理完成后，你的最终输出必须是一个 JSON 对象：
```json
{
  "action": "replied | drafted | silent",
  "classified_type": "work_question | urgent_issue | task_request | chat | noise",
  "topic": "消息主题",
  "project_name": "路由到的项目名（如有，否则 null）",
  "session_id": "route_to_session 返回的 session_id（非闲聊消息必填）",
  "task_context_id": null,
  "reply_content": "回复内容（如有）",
  "reason": "处理理由"
}
```

## 注意事项
- 每次通过 `send_feishu_message` 发送回复后，**必须**紧接着调用 `save_bot_reply` 保存回复到数据库
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

            # ── Force session routing BEFORE agent call ──
            # Don't rely on agent calling route_to_session — do it here.
            from datetime import timedelta
            from sqlalchemy import desc, select, func
            from models.db import Session as DBSession, SessionMessage

            session_context = None
            db_session_id = msg.session_id

            if not db_session_id and msg.chat_id:
                # Find recent session by chat_id within 2h window
                cutoff = datetime.now() - timedelta(hours=2)
                stmt = select(DBSession).where(
                    DBSession.source_chat_id == msg.chat_id,
                    DBSession.status.in_(["open", "waiting"]),
                    DBSession.last_active_at >= cutoff,
                ).order_by(desc(DBSession.last_active_at)).limit(1)
                result = await db.execute(stmt)
                recent_sess = result.scalar_one_or_none()

                if recent_sess:
                    db_session_id = recent_sess.id
                else:
                    # Create new session
                    new_sess = DBSession(
                        session_key=f"feishu_{msg.chat_id[:16]}_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
                        source_platform="feishu",
                        source_chat_id=msg.chat_id,
                        owner_user_id=msg.sender_id,
                        title=msg.content[:64] if msg.content else "新会话",
                        topic="",
                        project="",
                        priority="normal",
                        status="open",
                        summary_path="",
                        last_active_at=datetime.now(),
                        message_count=0,
                        risk_level="low",
                        needs_manual_review=False,
                    )
                    db.add(new_sess)
                    await db.commit()
                    await db.refresh(new_sess)
                    db_session_id = new_sess.id

            # Attach message to session
            if db_session_id:
                sess_obj = await db.get(DBSession, db_session_id)
                if sess_obj:
                    msg.session_id = db_session_id
                    sess_obj.message_count = sess_obj.message_count + 1
                    sess_obj.last_active_at = datetime.now()
                    sess_obj.updated_at = datetime.now()
                    await db.commit()

                    # Create session_message record
                    max_seq_result = await db.execute(
                        select(func.coalesce(func.max(SessionMessage.sequence_no), 0))
                        .where(SessionMessage.session_id == db_session_id)
                    )
                    max_seq = max_seq_result.scalar() or 0
                    db.add(SessionMessage(
                        session_id=db_session_id,
                        message_id=msg.id,
                        role="user",
                        sequence_no=max_seq + 1,
                    ))
                    await db.commit()

                    session_context = {
                        "agent_session_id": sess_obj.agent_session_id,
                        "project": sess_obj.project,
                        "title": sess_obj.title,
                        "db_session_id": sess_obj.id,
                    }

            # Build prompt
            prompt = _build_prompt(msg, session_context=session_context)

            # Audit: log the full prompt and session context before agent call
            db.add(AuditLog(
                event_type="pipeline_agent_call",
                target_type="message",
                target_id=str(msg.id),
                detail=json.dumps({
                    "message_id": msg.id,
                    "chat_id": msg.chat_id,
                    "session_context": session_context,
                    "prompt": prompt[:2000],
                }, ensure_ascii=False),
                operator="orchestrator",
            ))
            await db.commit()

            # Record agent run
            run = AgentRun(
                message_id=msg.id,
                agent_name="orchestrator",
                runtime_type="agent_sdk",
                status=AgentRunStatus.running,
                started_at=datetime.now(),
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

            # Audit: log agent result with session info
            db.add(AuditLog(
                event_type="pipeline_agent_result",
                target_type="message",
                target_id=str(msg.id),
                detail=json.dumps({
                    "message_id": msg.id,
                    "agent_session_id": agent_session_id,
                    "parsed_session_id": parsed.get("session_id"),
                    "action": parsed.get("action"),
                    "classified_type": parsed.get("classified_type"),
                    "result_text": result_text[:1500],
                }, ensure_ascii=False),
                operator="orchestrator",
            ))
            await db.commit()

            # Update message with classification
            msg.classified_type = parsed.get("classified_type", "chat")
            # session_id already set by pre-agent routing — don't overwrite with null
            msg.pipeline_status = PipelineStatus.completed
            msg.processed_at = datetime.now()
            await db.commit()

            # Save bot reply if agent sent one
            reply_content = parsed.get("reply_content", "")
            action = parsed.get("action", "silent")
            if reply_content and action in ("replied", "drafted"):
                await _save_reply(db, msg, reply_content,
                                  session_id=msg.session_id,
                                  is_draft=(action == "drafted"))

            # ── Ensure task_context exists for non-chat sessions ──
            if msg.session_id and msg.classified_type not in ("chat", "noise"):
                from models.db import TaskContext
                sess_for_tc = await db.get(DBSession, msg.session_id)
                if sess_for_tc and not sess_for_tc.task_context_id:
                    tc = TaskContext(
                        title=parsed.get("topic") or sess_for_tc.title or "未命名任务",
                        description="",
                        status="active",
                    )
                    db.add(tc)
                    await db.commit()
                    await db.refresh(tc)
                    sess_for_tc.task_context_id = tc.id
                    await db.commit()

            # Update agent run
            run.status = AgentRunStatus.success
            run.ended_at = datetime.now()
            run.output_path = result_text[:2000]
            run.session_id = msg.session_id
            run.cost_usd = result.get("cost_usd") or 0
            if agent_session_id:
                run.input_path = f"agent_session:{agent_session_id}"
                # Persist SDK session ID to the DB session for future resume
                if msg.session_id:
                    db_sess = await db.get(DBSession, msg.session_id)
                    if db_sess and not db_sess.agent_session_id:
                        db_sess.agent_session_id = agent_session_id
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
                    m.processed_at = datetime.now()
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

def _build_prompt(msg: Message, session_context: dict | None = None) -> str:
    """Build the orchestrator prompt from a message.

    Args:
        session_context: Optional dict with keys agent_session_id, project, title, db_session_id.
    """
    prompt = f"""新消息到达，请处理：

- 消息ID: {msg.id}
- 发送者: {msg.sender_name or msg.sender_id} (ID: {msg.sender_id})
- 聊天ID: {msg.chat_id}
- 消息类型: {msg.message_type}
- 时间: {msg.received_at.isoformat() if msg.received_at else 'unknown'}

消息内容:
{msg.content}"""

    if session_context:
        prompt += "\n\n## 已有会话上下文"
        if session_context.get("project"):
            prompt += f"\n- 关联项目: {session_context['project']}"
        if session_context.get("title"):
            prompt += f"\n- 会话标题: {session_context['title']}"
        if session_context.get("agent_session_id"):
            prompt += f"\n- agent_session_id: {session_context['agent_session_id']}（dispatch_to_project 时传入此 session_id 可恢复上下文）"
        if session_context.get("db_session_id"):
            prompt += f"\n- db_session_id: {session_context['db_session_id']}"

    return prompt


def _parse_result(text: str) -> dict:
    """Extract the JSON result from the agent's final output.

    Tries multiple strategies to find valid JSON containing the expected
    'action' key.  Falls back to a safe silent-action dict on failure.
    """
    text = text.strip()
    if not text:
        return _fallback_result("empty output")

    def _is_valid(d: dict) -> bool:
        return isinstance(d, dict) and "action" in d

    # Strategy 1: entire text is JSON
    try:
        d = json.loads(text)
        if _is_valid(d):
            return d
    except (json.JSONDecodeError, ValueError):
        pass

    # Strategy 2: fenced ```json ... ``` block
    if "```" in text:
        for block in text.split("```"):
            block = block.strip()
            if block.startswith("json"):
                block = block[4:].strip()
            try:
                d = json.loads(block)
                if _is_valid(d):
                    return d
            except (json.JSONDecodeError, ValueError):
                continue

    # Strategy 3: scan for balanced { ... } containing "action"
    # Walk backwards through all top-level brace pairs
    pos = len(text)
    while pos > 0:
        end = text.rfind("}", 0, pos)
        if end == -1:
            break
        # Find the matching opening brace by counting braces
        depth = 0
        start = end
        while start >= 0:
            if text[start] == "}":
                depth += 1
            elif text[start] == "{":
                depth -= 1
                if depth == 0:
                    break
            start -= 1
        if start >= 0 and depth == 0:
            candidate = text[start:end + 1]
            try:
                d = json.loads(candidate)
                if _is_valid(d):
                    return d
            except (json.JSONDecodeError, ValueError):
                pass
        pos = start  # try the next occurrence further left

    return _fallback_result("failed to parse structured output")


def _fallback_result(reason: str) -> dict:
    """Safe fallback — never uses raw output as reply content."""
    return {
        "action": "silent",
        "classified_type": "chat",
        "topic": "",
        "reply_content": "",
        "reason": reason,
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
        received_at=datetime.now(),
        classified_type="bot_reply",
        session_id=session_id,
        pipeline_status=PipelineStatus.completed,
        processed_at=datetime.now(),
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
