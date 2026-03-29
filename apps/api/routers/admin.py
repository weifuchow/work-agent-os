"""Admin API routes for the management dashboard."""

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Query, HTTPException
from fastapi.responses import StreamingResponse
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import func, select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import load_models_config, settings
from core.database import get_session
from core.orchestrator.claude_client import claude_client
from models.db import (
    AgentRun, AgentRunStatus, AuditLog, Message, Session, SessionMessage, Task,
    TaskContext, TaskContextStatus,
    PipelineStatus,
)

router = APIRouter(prefix="/api", tags=["admin"])


# ---------- Schemas ----------

class PaginatedResponse(BaseModel):
    items: list
    total: int
    page: int
    page_size: int


class PlaygroundRequest(BaseModel):
    messages: list[dict]
    system: str = ""
    model: str | None = None
    max_tokens: int = 4096
    stream: bool = False


# ---------- Messages ----------

@router.get("/messages")
async def list_messages(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    chat_id: Optional[str] = None,
    classified_type: Optional[str] = None,
    db: AsyncSession = Depends(get_session),
):
    query = select(Message)
    count_query = select(func.count(Message.id))

    if chat_id:
        query = query.where(Message.chat_id == chat_id)
        count_query = count_query.where(Message.chat_id == chat_id)
    if classified_type:
        query = query.where(Message.classified_type == classified_type)
        count_query = count_query.where(Message.classified_type == classified_type)

    total = (await db.execute(count_query)).scalar() or 0

    query = query.order_by(desc(Message.created_at)) \
        .offset((page - 1) * page_size) \
        .limit(page_size)
    results = (await db.execute(query)).scalars().all()

    return {
        "items": [_message_to_dict(m) for m in results],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/messages/{message_id}")
async def get_message(message_id: int, db: AsyncSession = Depends(get_session)):
    msg = await db.get(Message, message_id)
    if not msg:
        return {"error": "not found"}, 404
    return _message_to_dict(msg)


# ---------- Sessions ----------

@router.get("/sessions")
async def list_sessions(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: Optional[str] = None,
    db: AsyncSession = Depends(get_session),
):
    query = select(Session)
    count_query = select(func.count(Session.id))

    if status:
        query = query.where(Session.status == status)
        count_query = count_query.where(Session.status == status)

    total = (await db.execute(count_query)).scalar() or 0

    query = query.order_by(desc(Session.last_active_at)) \
        .offset((page - 1) * page_size) \
        .limit(page_size)
    results = (await db.execute(query)).scalars().all()

    return {
        "items": [_session_to_dict(s) for s in results],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/sessions/{session_id}")
async def get_session_detail(session_id: int, db: AsyncSession = Depends(get_session)):
    sess = await db.get(Session, session_id)
    if not sess:
        return {"error": "not found"}, 404

    # Get related messages
    sm_query = select(SessionMessage).where(
        SessionMessage.session_id == session_id
    ).order_by(SessionMessage.sequence_no)
    session_messages = (await db.execute(sm_query)).scalars().all()

    messages = []
    for sm in session_messages:
        msg = await db.get(Message, sm.message_id)
        if msg:
            messages.append({**_message_to_dict(msg), "role": sm.role, "sequence_no": sm.sequence_no})

    result = _session_to_dict(sess)
    result["messages"] = messages

    # Load summary content
    from core.sessions.summary import get_summary
    summary = await get_summary(sess)
    result["summary_content"] = summary

    return result


# ---------- Audit Logs ----------

@router.get("/audit-logs")
async def list_audit_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    event_type: Optional[str] = None,
    db: AsyncSession = Depends(get_session),
):
    query = select(AuditLog)
    count_query = select(func.count(AuditLog.id))

    if event_type:
        query = query.where(AuditLog.event_type == event_type)
        count_query = count_query.where(AuditLog.event_type == event_type)

    total = (await db.execute(count_query)).scalar() or 0

    query = query.order_by(desc(AuditLog.created_at)) \
        .offset((page - 1) * page_size) \
        .limit(page_size)
    results = (await db.execute(query)).scalars().all()

    return {
        "items": [_audit_to_dict(a) for a in results],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ---------- Stats ----------

@router.get("/stats")
async def get_stats(db: AsyncSession = Depends(get_session)):
    msg_count = (await db.execute(select(func.count(Message.id)))).scalar() or 0
    session_count = (await db.execute(select(func.count(Session.id)))).scalar() or 0
    task_count = (await db.execute(select(func.count(Task.id)))).scalar() or 0
    audit_count = (await db.execute(select(func.count(AuditLog.id)))).scalar() or 0

    # Classification breakdown
    classified = (await db.execute(
        select(Message.classified_type, func.count(Message.id))
        .group_by(Message.classified_type)
    )).all()

    return {
        "messages": msg_count,
        "sessions": session_count,
        "tasks": task_count,
        "audit_logs": audit_count,
        "classification": {(row[0] or "unclassified"): row[1] for row in classified},
    }


@router.get("/models")
async def list_models():
    return load_models_config()


# ---------- Playground ----------

@router.post("/playground/chat")
async def playground_chat(req: PlaygroundRequest, db: AsyncSession = Depends(get_session)):
    # Record the run
    run = AgentRun(
        agent_name="playground",
        runtime_type="claude_api",
        input_path="",
        output_path="",
        status=AgentRunStatus.running,
        started_at=datetime.now(),
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)

    if req.stream:
        async def event_stream():
            full_response = []
            try:
                async for chunk in claude_client.chat_stream(
                    messages=req.messages,
                    system=req.system,
                    model=req.model,
                    max_tokens=req.max_tokens,
                ):
                    full_response.append(chunk)
                    yield f"data: {json.dumps({'text': chunk})}\n\n"

                # Mark success
                run.status = AgentRunStatus.success
                run.ended_at = datetime.now()
                db.add(run)
                await db.commit()

                yield f"data: {json.dumps({'done': True})}\n\n"
            except Exception as e:
                run.status = AgentRunStatus.failed
                run.error_message = str(e)
                run.ended_at = datetime.now()
                db.add(run)
                await db.commit()
                yield f"data: {json.dumps({'error': str(e)})}\n\n"

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    # Non-streaming
    try:
        text = await claude_client.chat(
            messages=req.messages,
            system=req.system,
            model=req.model,
            max_tokens=req.max_tokens,
        )
        run.status = AgentRunStatus.success
        run.ended_at = datetime.now()
        db.add(run)
        await db.commit()
        return {"text": text, "run_id": run.id}
    except Exception as e:
        run.status = AgentRunStatus.failed
        run.error_message = str(e)
        run.ended_at = datetime.now()
        db.add(run)
        await db.commit()
        return {"error": str(e), "run_id": run.id}


# ---------- Agent (Agent SDK) ----------

class AgentRunRequest(BaseModel):
    prompt: str
    system_prompt: str = ""
    max_turns: int = 30
    skill: Optional[str] = None
    session_id: Optional[str] = None


@router.get("/agent/skills")
async def list_skills():
    """List available agent skills."""
    from skills import SKILL_DESCRIPTIONS
    return {"skills": [{"name": k, "description": v} for k, v in SKILL_DESCRIPTIONS.items()]}


@router.post("/agent/run")
async def agent_run_stream(req: AgentRunRequest, db: AsyncSession = Depends(get_session)):
    """Run a Claude Agent SDK task with SSE streaming events."""
    from core.orchestrator.agent_client import agent_client

    # Record the run
    run = AgentRun(
        agent_name=req.skill or "agent_sdk",
        runtime_type="agent_sdk",
        input_path="",
        output_path="",
        status=AgentRunStatus.running,
        started_at=datetime.now(),
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)

    async def event_stream():
        try:
            async for event in agent_client.run_stream(
                prompt=req.prompt,
                system_prompt=req.system_prompt,
                max_turns=req.max_turns,
                session_id=req.session_id,
                skill=req.skill,
            ):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

            # Mark success
            run.status = AgentRunStatus.success
            run.ended_at = datetime.now()
            db.add(run)
            await db.commit()
        except Exception as e:
            logger.exception("Agent run failed: {}", e)
            run.status = AgentRunStatus.failed
            run.error_message = str(e)
            run.ended_at = datetime.now()
            db.add(run)
            await db.commit()
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)}, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.get("/agent/sessions")
async def list_agent_sessions():
    """List Agent SDK sessions."""
    from core.orchestrator.agent_client import agent_client
    sessions = await agent_client.list_sessions()
    return {"sessions": sessions}


@router.delete("/agent/sessions/{sid}")
async def delete_agent_session(sid: str):
    """Delete an Agent SDK session."""
    from core.orchestrator.agent_client import agent_client
    await agent_client.delete_session(sid)
    return {"success": True}


@router.get("/agent/sessions/{sid}/transcript")
async def get_agent_transcript(sid: str):
    """Get full transcript for an Agent SDK session (including subagent sidechains)."""
    from core.orchestrator.agent_client import agent_client
    messages = await agent_client.get_session_messages(sid)
    return {"session_id": sid, "messages": messages}


# ---------- Pipeline ----------

@router.post("/messages/{message_id}/process")
async def process_message_endpoint(message_id: int, db: AsyncSession = Depends(get_session)):
    """Manually trigger pipeline processing for a message."""
    msg = await db.get(Message, message_id)
    if not msg:
        return {"error": "not found"}, 404

    import asyncio
    from core.pipeline import process_message
    asyncio.create_task(process_message(message_id))
    return {"status": "scheduled", "message_id": message_id}


@router.post("/messages/{message_id}/reprocess")
async def reprocess_message_endpoint(message_id: int, db: AsyncSession = Depends(get_session)):
    """Reset and reprocess a message through the pipeline."""
    msg = await db.get(Message, message_id)
    if not msg:
        return {"error": "not found"}, 404

    import asyncio
    from core.pipeline import reprocess_message
    asyncio.create_task(reprocess_message(message_id))
    return {"status": "scheduled", "message_id": message_id}


@router.post("/pipeline/process-pending")
async def process_pending_messages(limit: int = Query(10, ge=1, le=100)):
    """Process all pending (unprocessed) messages."""
    import asyncio
    from core.pipeline import process_message
    from core.database import async_session_factory

    async with async_session_factory() as db:
        stmt = (
            select(Message.id)
            .where(Message.pipeline_status == PipelineStatus.pending)
            .order_by(Message.created_at)
            .limit(limit)
        )
        result = await db.execute(stmt)
        msg_ids = [row[0] for row in result.all()]

    for mid in msg_ids:
        asyncio.create_task(process_message(mid))

    return {"scheduled": len(msg_ids), "message_ids": msg_ids}


@router.get("/pipeline/stats")
async def pipeline_stats(db: AsyncSession = Depends(get_session)):
    """Get pipeline processing statistics."""
    from sqlalchemy import case

    stmt = select(
        Message.pipeline_status,
        func.count(Message.id),
    ).group_by(Message.pipeline_status)
    result = await db.execute(stmt)
    breakdown = {row[0] or "pending": row[1] for row in result.all()}

    return {"pipeline_status": breakdown}


@router.post("/sessions/lifecycle-check")
async def run_lifecycle_check():
    """Manually trigger session lifecycle check."""
    from core.sessions.lifecycle import run_lifecycle_check
    counts = await run_lifecycle_check()
    return counts


@router.get("/sessions/{session_id}/status")
async def get_session_task_status(session_id: int):
    """Get task progress for a session. NO LLM — pure DB query."""
    from core.monitor import get_task_status_text
    text = await get_task_status_text(session_id)
    return {"session_id": session_id, "status_text": text}


@router.get("/conversations")
async def list_conversations(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    chat_id: Optional[str] = None,
    db: AsyncSession = Depends(get_session),
):
    """List conversations — pairs of user messages and bot replies."""
    # Get user messages (not bot replies)
    query = select(Message).where(Message.sender_id != "bot")
    count_query = select(func.count(Message.id)).where(Message.sender_id != "bot")

    if chat_id:
        query = query.where(Message.chat_id == chat_id)
        count_query = count_query.where(Message.chat_id == chat_id)

    total = (await db.execute(count_query)).scalar() or 0
    query = query.order_by(desc(Message.created_at)).offset((page - 1) * page_size).limit(page_size)
    user_msgs = (await db.execute(query)).scalars().all()

    conversations = []
    for msg in user_msgs:
        # Find corresponding bot reply
        reply_stmt = select(Message).where(
            Message.platform_message_id == f"reply_{msg.platform_message_id}"
        ).order_by(desc(Message.created_at)).limit(1)
        reply = (await db.execute(reply_stmt)).scalar_one_or_none()

        conversations.append({
            "user_message": _message_to_dict(msg),
            "bot_reply": _message_to_dict(reply) if reply else None,
            "session_id": msg.session_id,
        })

    return {"items": conversations, "total": total, "page": page, "page_size": page_size}


@router.get("/conversations/{chat_id}/history")
async def get_chat_history(
    chat_id: str,
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_session),
):
    """Get full chat history for a specific chat_id, ordered chronologically."""
    stmt = (
        select(Message)
        .where(Message.chat_id == chat_id)
        .order_by(Message.created_at)
        .limit(limit)
    )
    messages = (await db.execute(stmt)).scalars().all()
    return {
        "chat_id": chat_id,
        "messages": [
            {**_message_to_dict(m), "role": "assistant" if m.sender_id == "bot" else "user"}
            for m in messages
        ],
    }


@router.get("/token-usage")
async def get_token_usage(
    days: int = Query(7, ge=1, le=90),
    db: AsyncSession = Depends(get_session),
):
    """Get token consumption statistics."""
    cutoff = datetime.now() - timedelta(days=days)

    # Total tokens
    stmt = select(
        func.sum(AgentRun.input_tokens),
        func.sum(AgentRun.output_tokens),
        func.count(AgentRun.id),
    ).where(AgentRun.started_at >= cutoff)
    row = (await db.execute(stmt)).one()
    total_input = row[0] or 0
    total_output = row[1] or 0
    total_runs = row[2] or 0

    # Per agent breakdown
    stmt = select(
        AgentRun.agent_name,
        func.sum(AgentRun.input_tokens),
        func.sum(AgentRun.output_tokens),
        func.count(AgentRun.id),
    ).where(AgentRun.started_at >= cutoff).group_by(AgentRun.agent_name)
    breakdown = (await db.execute(stmt)).all()

    # Daily totals
    # Use date string grouping for SQLite compatibility
    stmt = select(
        func.date(AgentRun.started_at),
        func.sum(AgentRun.input_tokens),
        func.sum(AgentRun.output_tokens),
        func.count(AgentRun.id),
    ).where(AgentRun.started_at >= cutoff).group_by(func.date(AgentRun.started_at))
    daily = (await db.execute(stmt)).all()

    return {
        "period_days": days,
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "total_tokens": total_input + total_output,
        "total_runs": total_runs,
        "by_agent": [
            {"agent": r[0], "input_tokens": r[1] or 0, "output_tokens": r[2] or 0, "runs": r[3]}
            for r in breakdown
        ],
        "daily": [
            {"date": str(r[0]), "input_tokens": r[1] or 0, "output_tokens": r[2] or 0, "runs": r[3]}
            for r in daily
        ],
    }


@router.post("/reports/daily")
async def generate_daily_report_endpoint(
    target_date: Optional[str] = None,
    push_chat_id: str = "",
):
    """Manually trigger daily report generation."""
    from core.reports.daily import generate_daily_report
    content = await generate_daily_report(
        target_date=target_date,
        push_to_feishu=bool(push_chat_id),
        push_chat_id=push_chat_id,
    )
    return {"report": content[:3000]}


@router.post("/memory/consolidate")
async def consolidate_memory_endpoint():
    """Manually trigger memory consolidation."""
    from core.memory.consolidator import consolidate_memories
    result = await consolidate_memories()
    return result


# ---------- Task Contexts ----------

class TaskContextUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None


@router.get("/task-contexts")
async def list_task_contexts(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: Optional[str] = None,
    db: AsyncSession = Depends(get_session),
):
    """List task contexts with their associated sessions."""
    query = select(TaskContext)
    count_query = select(func.count(TaskContext.id))

    if status:
        query = query.where(TaskContext.status == status)
        count_query = count_query.where(TaskContext.status == status)

    total = (await db.execute(count_query)).scalar() or 0
    query = query.order_by(desc(TaskContext.updated_at)) \
        .offset((page - 1) * page_size).limit(page_size)
    task_contexts = (await db.execute(query)).scalars().all()

    items = []
    for tc in task_contexts:
        # Fetch associated sessions
        sess_stmt = select(Session).where(
            Session.task_context_id == tc.id
        ).order_by(desc(Session.last_active_at))
        sessions = (await db.execute(sess_stmt)).scalars().all()

        items.append({
            **_task_context_to_dict(tc),
            "sessions": [_session_to_dict(s) for s in sessions],
            "session_count": len(sessions),
        })

    # Also fetch unlinked sessions
    unlinked_stmt = select(Session).where(
        Session.task_context_id == None  # noqa: E711
    ).order_by(desc(Session.last_active_at))
    unlinked = (await db.execute(unlinked_stmt)).scalars().all()

    return {
        "items": items,
        "unlinked_sessions": [_session_to_dict(s) for s in unlinked],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/task-contexts/{task_context_id}")
async def get_task_context(task_context_id: int, db: AsyncSession = Depends(get_session)):
    """Get task context detail with all sessions and their messages."""
    tc = await db.get(TaskContext, task_context_id)
    if not tc:
        raise HTTPException(status_code=404, detail="not found")

    sess_stmt = select(Session).where(
        Session.task_context_id == tc.id
    ).order_by(desc(Session.last_active_at))
    sessions = (await db.execute(sess_stmt)).scalars().all()

    sessions_with_messages = []
    for s in sessions:
        sm_stmt = select(SessionMessage).where(
            SessionMessage.session_id == s.id
        ).order_by(SessionMessage.sequence_no)
        sms = (await db.execute(sm_stmt)).scalars().all()

        messages = []
        for sm in sms:
            msg = await db.get(Message, sm.message_id)
            if msg:
                messages.append({**_message_to_dict(msg), "role": sm.role, "sequence_no": sm.sequence_no})

        sessions_with_messages.append({
            **_session_to_dict(s),
            "messages": messages,
        })

    return {
        **_task_context_to_dict(tc),
        "sessions": sessions_with_messages,
        "session_count": len(sessions),
    }


@router.put("/task-contexts/{task_context_id}")
async def update_task_context(
    task_context_id: int,
    body: TaskContextUpdate,
    db: AsyncSession = Depends(get_session),
):
    tc = await db.get(TaskContext, task_context_id)
    if not tc:
        raise HTTPException(status_code=404, detail="not found")

    if body.title is not None:
        tc.title = body.title
    if body.description is not None:
        tc.description = body.description
    if body.status is not None:
        tc.status = body.status
    tc.updated_at = datetime.now()
    await db.commit()
    return _task_context_to_dict(tc)


# ---------- Projects ----------

@router.get("/projects")
async def list_projects_endpoint():
    """List all registered projects from data/projects.yaml."""
    from core.projects import get_projects, merge_skills
    from skills import SKILL_REGISTRY

    projects = get_projects()
    result = []
    for p in projects:
        merged = merge_skills(SKILL_REGISTRY, p.path)
        project_only = [k for k in merged if k not in SKILL_REGISTRY]
        result.append({
            "name": p.name,
            "path": str(p.path),
            "description": p.description,
            "path_exists": p.path.exists(),
            "has_skills": bool(project_only),
            "skills": project_only,
        })
    return {"projects": result}


@router.post("/projects/refresh")
async def refresh_projects_endpoint():
    """Reload projects.yaml from disk (clears cache)."""
    from core.projects import refresh_projects
    projects = refresh_projects()
    return {"refreshed": len(projects), "projects": [p.name for p in projects]}


# ---------- Memory Files ----------

class MemoryFileUpdate(BaseModel):
    content: str


def _memory_base_dir() -> Path:
    from core.config import settings
    return Path(settings.memory_dir)


def _validate_memory_path(file_path: str) -> Path:
    """Validate and resolve a memory file path, preventing traversal."""
    base = _memory_base_dir().resolve()
    target = (base / file_path).resolve()
    if not str(target).startswith(str(base)):
        raise HTTPException(status_code=400, detail="invalid path")
    return target


@router.get("/memory/files")
async def list_memory_files():
    """List all memory files under data/memory/."""
    base = _memory_base_dir()
    if not base.exists():
        return {"files": []}

    files = []
    for p in sorted(base.rglob("*.md")):
        rel = p.relative_to(base)
        parts = rel.parts
        category = parts[0] if len(parts) > 1 else "general"
        stat = p.stat()
        files.append({
            "path": str(rel).replace("\\", "/"),
            "category": category,
            "name": p.stem,
            "size": stat.st_size,
            "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
        })
    return {"files": files}


@router.get("/memory/files/{file_path:path}")
async def read_memory_file(file_path: str):
    """Read a memory file's content."""
    target = _validate_memory_path(file_path)
    if not target.exists():
        raise HTTPException(status_code=404, detail="file not found")
    content = target.read_text(encoding="utf-8")
    return {"path": file_path, "content": content}


@router.put("/memory/files/{file_path:path}")
async def update_memory_file(file_path: str, body: MemoryFileUpdate):
    """Update or create a memory file."""
    target = _validate_memory_path(file_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body.content, encoding="utf-8")
    return {"path": file_path, "size": len(body.content.encode("utf-8"))}


@router.delete("/memory/files/{file_path:path}")
async def delete_memory_file(file_path: str):
    """Delete a memory file."""
    target = _validate_memory_path(file_path)
    if not target.exists():
        raise HTTPException(status_code=404, detail="file not found")
    target.unlink()
    return {"deleted": file_path}

def _message_to_dict(m: Message) -> dict:
    return {
        "id": m.id,
        "platform": m.platform,
        "platform_message_id": m.platform_message_id,
        "chat_id": m.chat_id,
        "sender_id": m.sender_id,
        "sender_name": m.sender_name,
        "message_type": m.message_type,
        "content": m.content,
        "classified_type": m.classified_type,
        "session_id": m.session_id,
        "pipeline_status": m.pipeline_status,
        "pipeline_error": m.pipeline_error,
        "processed_at": m.processed_at.isoformat() if m.processed_at else None,
        "sent_at": m.sent_at.isoformat() if m.sent_at else None,
        "received_at": m.received_at.isoformat() if m.received_at else None,
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }


def _session_to_dict(s: Session) -> dict:
    return {
        "id": s.id,
        "session_key": s.session_key,
        "source_platform": s.source_platform,
        "source_chat_id": s.source_chat_id,
        "owner_user_id": s.owner_user_id,
        "title": s.title,
        "topic": s.topic,
        "project": s.project,
        "priority": s.priority,
        "status": s.status,
        "summary_path": s.summary_path,
        "last_active_at": s.last_active_at.isoformat() if s.last_active_at else None,
        "message_count": s.message_count,
        "risk_level": s.risk_level,
        "needs_manual_review": s.needs_manual_review,
        "task_context_id": s.task_context_id,
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
    }


def _task_context_to_dict(tc: TaskContext) -> dict:
    return {
        "id": tc.id,
        "title": tc.title,
        "description": tc.description,
        "status": tc.status,
        "created_at": tc.created_at.isoformat() if tc.created_at else None,
        "updated_at": tc.updated_at.isoformat() if tc.updated_at else None,
    }


def _audit_to_dict(a: AuditLog) -> dict:
    return {
        "id": a.id,
        "event_type": a.event_type,
        "target_type": a.target_type,
        "target_id": a.target_id,
        "detail": a.detail,
        "operator": a.operator,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    }
