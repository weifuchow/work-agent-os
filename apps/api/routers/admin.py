"""Admin API routes for the management dashboard."""

import json
from datetime import UTC, datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import func, select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import get_session
from core.orchestrator.claude_client import claude_client
from models.db import (
    AgentRun, AgentRunStatus, AuditLog, Message, Session, SessionMessage, Task,
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
    model: str = "claude-sonnet-4-6"
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
        started_at=datetime.now(UTC),
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
                run.ended_at = datetime.now(UTC)
                db.add(run)
                await db.commit()

                yield f"data: {json.dumps({'done': True})}\n\n"
            except Exception as e:
                run.status = AgentRunStatus.failed
                run.error_message = str(e)
                run.ended_at = datetime.now(UTC)
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
        run.ended_at = datetime.now(UTC)
        db.add(run)
        await db.commit()
        return {"text": text, "run_id": run.id}
    except Exception as e:
        run.status = AgentRunStatus.failed
        run.error_message = str(e)
        run.ended_at = datetime.now(UTC)
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
        started_at=datetime.now(UTC),
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
            run.ended_at = datetime.now(UTC)
            db.add(run)
            await db.commit()
        except Exception as e:
            logger.exception("Agent run failed: {}", e)
            run.status = AgentRunStatus.failed
            run.error_message = str(e)
            run.ended_at = datetime.now(UTC)
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
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
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
