"""Task Linker — use LLM to associate sessions with task contexts."""

import json
from datetime import datetime

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.db import Session, TaskContext, TaskContextStatus


async def link_session_to_task(db: AsyncSession, session: Session) -> TaskContext:
    """Analyze a session and link it to an existing or new TaskContext.

    Uses LLM to determine semantic similarity between the session's
    topic/project and existing active task contexts.
    """
    # If already linked, return existing
    if session.task_context_id:
        existing = await db.get(TaskContext, session.task_context_id)
        if existing:
            return existing

    # Fetch recent active task contexts
    stmt = (
        select(TaskContext)
        .where(TaskContext.status == TaskContextStatus.active)
        .order_by(TaskContext.updated_at.desc())
        .limit(20)
    )
    existing_tasks = (await db.execute(stmt)).scalars().all()

    # Build context for LLM
    tasks_desc = []
    for t in existing_tasks:
        tasks_desc.append({"id": t.id, "title": t.title, "description": t.description})

    match_result = await _llm_match(session, tasks_desc)
    matched_id = match_result.get("match_task_id")
    new_title = match_result.get("new_title", "")

    if matched_id:
        # Link to existing task context
        task_ctx = await db.get(TaskContext, matched_id)
        if task_ctx:
            session.task_context_id = task_ctx.id
            task_ctx.updated_at = datetime.now()
            await db.commit()
            logger.info("TaskLinker: session {} → existing task {} ({})",
                        session.id, task_ctx.id, task_ctx.title)
            return task_ctx

    # Create new task context
    task_ctx = TaskContext(
        title=new_title or session.title or session.topic or "未命名任务",
        description=match_result.get("reason", ""),
        status=TaskContextStatus.active,
    )
    db.add(task_ctx)
    await db.commit()
    await db.refresh(task_ctx)

    session.task_context_id = task_ctx.id
    await db.commit()

    logger.info("TaskLinker: session {} → new task {} ({})",
                session.id, task_ctx.id, task_ctx.title)
    return task_ctx


async def _llm_match(session: Session, existing_tasks: list[dict]) -> dict:
    """Ask LLM to match session to an existing task or suggest a new one."""
    from core.orchestrator.claude_client import claude_client

    system_prompt = """你是任务上下文分析器。判断一个新会话是否属于已有的任务上下文。

输出严格 JSON，不要多余文字：
{
  "match_task_id": <已有任务ID> 或 null（无匹配时）,
  "new_title": "如果是新任务，给出简洁标题（10字以内）",
  "reason": "判断理由（一句话）"
}

匹配标准：
- 相同项目且相关主题 → 匹配
- 相似的技术问题或工作任务 → 匹配
- 不同项目或完全无关主题 → 不匹配，创建新任务"""

    tasks_text = "无" if not existing_tasks else json.dumps(existing_tasks, ensure_ascii=False)

    prompt = f"""当前会话信息：
- 标题: {session.title}
- 主题: {session.topic}
- 项目: {session.project}
- 来源: {session.source_chat_id}

已有任务上下文列表：
{tasks_text}"""

    try:
        text = await claude_client.chat(
            messages=[{"role": "user", "content": prompt}],
            system=system_prompt,
            max_tokens=256,
        )
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        result = json.loads(text)
        # Validate match_task_id is in existing tasks
        if result.get("match_task_id"):
            valid_ids = {t["id"] for t in existing_tasks}
            if result["match_task_id"] not in valid_ids:
                result["match_task_id"] = None
        return result
    except Exception as e:
        logger.warning("TaskLinker LLM failed: {}", e)
        return {"match_task_id": None, "new_title": session.title or session.topic, "reason": f"LLM分析失败: {e}"}
