"""Admin API routes for the management dashboard."""

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, HTTPException
from fastapi.responses import StreamingResponse
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import func, select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from core.analytics.projects import build_project_summary_context, get_project_insights
from core.config import (
    filter_models_for_runtime,
    load_models_config,
    get_agent_runtime_override,
    get_model_override,
    set_agent_runtime_override,
    set_model_override,
    settings,
    with_model_state,
)
from core.database import get_session
from core.memory.store import (
    create_memory_entry,
    delete_memory_entry as delete_structured_memory_entry,
    ensure_memory_bootstrap,
    get_memory_entry,
    list_memory_entries,
    memory_entry_to_dict,
    update_memory_entry as update_structured_memory_entry,
)
from core.orchestrator.agent_runtime import (
    DEFAULT_AGENT_RUNTIME,
    SUPPORTED_AGENT_RUNTIMES,
    get_agent_run_runtime_type,
    normalize_agent_runtime,
)
from core.orchestrator.claude_client import claude_client
from models.db import (
    AgentRun, AgentRunStatus, AuditLog, MemoryEntry, Message, Session, SessionMessage, Task,
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


class MemoryEntryBase(BaseModel):
    scope: str = "general"
    project_name: str = ""
    project_version: str = ""
    project_branch: str = ""
    project_commit_sha: str = ""
    project_commit_time: datetime | None = None
    category: str = "note"
    title: str = ""
    content: str = ""
    tags: list[str] = []
    source_type: str = "manual"
    source_session_id: int | None = None
    source_message_id: int | None = None
    importance: int = 3
    happened_at: datetime | None = None
    valid_until: datetime | None = None
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    occurrence_count: int = 1


class MemoryEntryCreate(MemoryEntryBase):
    title: str
    content: str


class MemoryEntryUpdate(BaseModel):
    scope: str | None = None
    project_name: str | None = None
    project_version: str | None = None
    project_branch: str | None = None
    project_commit_sha: str | None = None
    project_commit_time: datetime | None = None
    category: str | None = None
    title: str | None = None
    content: str | None = None
    tags: list[str] | None = None
    source_type: str | None = None
    source_session_id: int | None = None
    source_message_id: int | None = None
    importance: int | None = None
    happened_at: datetime | None = None
    valid_until: datetime | None = None
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    occurrence_count: int | None = None


class ProjectSummaryRequest(BaseModel):
    project_name: str = ""
    days: int = 30


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

    query = query.order_by(desc(AuditLog.id)) \
        .offset((page - 1) * page_size) \
        .limit(page_size)
    results = (await db.execute(query)).scalars().all()

    return {
        "items": [_audit_to_dict(a) for a in results],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ---------- Agent Runs ----------

@router.get("/agent-runs/inflight")
async def get_inflight_runs(db: AsyncSession = Depends(get_session)):
    """Get all currently running agent tasks — real-time observability."""
    stmt = select(AgentRun).where(AgentRun.status == AgentRunStatus.running)
    runs = (await db.execute(stmt)).scalars().all()

    now = datetime.now()
    items = []
    for r in runs:
        elapsed = (now - r.started_at).total_seconds() if r.started_at else 0
        items.append({
            "id": r.id,
            "agent_name": r.agent_name,
            "runtime_type": r.runtime_type,
            "message_id": r.message_id,
            "session_id": r.session_id,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "elapsed_seconds": round(elapsed, 1),
        })

    return {"items": items, "total_running": len(items)}


@router.get("/agent-runs")
async def list_agent_runs(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: Optional[str] = None,
    agent_name: Optional[str] = None,
    db: AsyncSession = Depends(get_session),
):
    """List agent run history with filtering."""
    query = select(AgentRun)
    count_query = select(func.count(AgentRun.id))

    if status:
        query = query.where(AgentRun.status == status)
        count_query = count_query.where(AgentRun.status == status)
    if agent_name:
        query = query.where(AgentRun.agent_name == agent_name)
        count_query = count_query.where(AgentRun.agent_name == agent_name)

    total = (await db.execute(count_query)).scalar() or 0

    query = query.order_by(desc(AgentRun.id)) \
        .offset((page - 1) * page_size) \
        .limit(page_size)
    runs = (await db.execute(query)).scalars().all()

    items = []
    for r in runs:
        duration = None
        if r.started_at and r.ended_at:
            duration = round((r.ended_at - r.started_at).total_seconds(), 1)
        elif r.started_at and r.status == AgentRunStatus.running:
            duration = round((datetime.now() - r.started_at).total_seconds(), 1)

        items.append({
            "id": r.id,
            "agent_name": r.agent_name,
            "runtime_type": r.runtime_type,
            "message_id": r.message_id,
            "session_id": r.session_id,
            "status": r.status,
            "cost_usd": r.cost_usd,
            "input_tokens": r.input_tokens,
            "output_tokens": r.output_tokens,
            "duration_seconds": duration,
            "error_message": r.error_message or None,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "ended_at": r.ended_at.isoformat() if r.ended_at else None,
        })

    return {"items": items, "total": total, "page": page, "page_size": page_size}


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


@router.get("/projects/insights")
async def get_project_insights_endpoint(
    days: int = Query(30, ge=1, le=365),
    db: AsyncSession = Depends(get_session),
):
    await ensure_memory_bootstrap(db)
    return await get_project_insights(db, days=days)


@router.post("/projects/insights/summary")
async def generate_project_summary(
    body: ProjectSummaryRequest,
    db: AsyncSession = Depends(get_session),
):
    await ensure_memory_bootstrap(db)
    context = await build_project_summary_context(db, body.project_name, days=body.days)
    detail = context["detail"]

    system_prompt = """你是项目运营分析助手。你的任务是根据项目相关会话、统计分布与热点主题，
输出一段简洁但有决策价值的中文总结。

要求：
- 重点指出高频问题、重复出现的主题、近期活跃点
- 如果是非项目闲聊/个人偏好，也要总结用户稳定偏好和交流倾向
- 明确指出哪些问题值得继续沉淀为记忆或形成标准答复
- 用 3-6 个项目符号，直接给结论，不要寒暄"""

    prompt = json.dumps({
        "project_name": body.project_name,
        "period_days": body.days,
        "detail": detail,
        "sessions": context["sessions"],
        "hot_topics": context["hot_topics"][:8],
    }, ensure_ascii=False, indent=2)

    try:
        text = await claude_client.chat(
            messages=[{"role": "user", "content": prompt}],
            system=system_prompt,
            max_tokens=1024,
        )
        return {
            "project_name": body.project_name,
            "period_days": body.days,
            "summary": text.strip(),
            "generated_at": datetime.now().isoformat(),
        }
    except Exception as e:
        logger.warning("Project summary generation failed for {}: {}", body.project_name, e)
        fallback_topics = detail.get("top_topics", [])[:3]
        fallback_text = "\n".join(
            f"- 高频主题：{item['topic']}（{item['count']} 次）"
            for item in fallback_topics
        ) or "- 当前没有足够的数据生成问题总结。"
        return {
            "project_name": body.project_name,
            "period_days": body.days,
            "summary": fallback_text,
            "generated_at": datetime.now().isoformat(),
            "fallback": True,
        }


@router.get("/models")
async def list_models(runtime: Optional[str] = Query(None)):
    resolved_runtime = normalize_agent_runtime(
        runtime or get_agent_runtime_override() or settings.default_agent_runtime or DEFAULT_AGENT_RUNTIME
    )
    config = filter_models_for_runtime(load_models_config(), resolved_runtime)
    return with_model_state(config, get_model_override(resolved_runtime), resolved_runtime)


class ModelSwitchRequest(BaseModel):
    model: str
    runtime: Optional[str] = None


@router.post("/model/switch")
async def switch_model(req: ModelSwitchRequest, db: AsyncSession = Depends(get_session)):
    """Switch the runtime model override."""
    runtime = normalize_agent_runtime(
        req.runtime or get_agent_runtime_override() or settings.default_agent_runtime or DEFAULT_AGENT_RUNTIME
    )
    config = filter_models_for_runtime(load_models_config(), runtime)
    old_model = get_model_override(runtime) or config.get("default", "unknown")
    set_model_override(req.model, runtime=runtime)

    db.add(AuditLog(
        event_type="model_switch",
        target_type="model",
        target_id=req.model,
        detail=json.dumps({
            "old_model": old_model,
            "new_model": req.model,
            "runtime": runtime,
            "source": "admin_api",
        }, ensure_ascii=False),
        operator="admin",
    ))
    await db.commit()

    return {"old_model": old_model, "new_model": req.model, "current": req.model, "runtime": runtime}


class AgentRuntimeSwitchRequest(BaseModel):
    runtime: str


@router.get("/agent/runtime")
async def get_agent_runtime():
    override = get_agent_runtime_override()
    current = normalize_agent_runtime(
        override or settings.default_agent_runtime or DEFAULT_AGENT_RUNTIME
    )
    return {
        "supported": list(SUPPORTED_AGENT_RUNTIMES),
        "current": current,
        "override": override,
    }


@router.post("/agent/runtime")
async def switch_agent_runtime(req: AgentRuntimeSwitchRequest, db: AsyncSession = Depends(get_session)):
    runtime = normalize_agent_runtime(req.runtime)
    old_runtime = normalize_agent_runtime(
        get_agent_runtime_override() or settings.default_agent_runtime or DEFAULT_AGENT_RUNTIME
    )
    set_agent_runtime_override(runtime)

    db.add(AuditLog(
        event_type="agent_runtime_switch",
        target_type="agent_runtime",
        target_id=runtime,
        detail=json.dumps({
            "old_runtime": old_runtime,
            "new_runtime": runtime,
            "source": "admin_api",
        }, ensure_ascii=False),
        operator="admin",
    ))
    await db.commit()

    return {"old_runtime": old_runtime, "new_runtime": runtime, "current": runtime}


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
    runtime: Optional[str] = None


@router.get("/agent/skills")
async def list_skills():
    """List available agent skills."""
    from skills import SKILL_DESCRIPTIONS
    return {"skills": [{"name": k, "description": v} for k, v in SKILL_DESCRIPTIONS.items()]}


@router.post("/agent/run")
async def agent_run_stream(req: AgentRunRequest, db: AsyncSession = Depends(get_session)):
    """Run an agent task with SSE streaming events."""
    from core.orchestrator.agent_client import agent_client
    runtime = normalize_agent_runtime(
        req.runtime or get_agent_runtime_override() or settings.default_agent_runtime or DEFAULT_AGENT_RUNTIME
    )

    # Record the run
    run = AgentRun(
        agent_name=req.skill or "agent_sdk",
        runtime_type=get_agent_run_runtime_type(runtime),
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
                runtime=runtime,
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
async def list_agent_sessions(runtime: Optional[str] = Query(None)):
    """List agent sessions."""
    from core.orchestrator.agent_client import agent_client
    sessions = await agent_client.list_sessions(runtime=runtime)
    return {"sessions": sessions}


@router.delete("/agent/sessions/{sid}")
async def delete_agent_session(sid: str, runtime: str = Query(DEFAULT_AGENT_RUNTIME)):
    """Delete an agent session."""
    from core.orchestrator.agent_client import agent_client
    await agent_client.delete_session(sid, runtime=runtime)
    return {"success": True}


@router.get("/agent/sessions/{sid}/transcript")
async def get_agent_transcript(sid: str, runtime: str = Query(DEFAULT_AGENT_RUNTIME)):
    """Get full transcript for an agent session."""
    from core.orchestrator.agent_client import agent_client
    messages = await agent_client.get_session_messages(sid, runtime=runtime)
    return {"session_id": sid, "runtime": normalize_agent_runtime(runtime), "messages": messages}


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


# ---------- Triage ----------

@router.get("/triage/runs")
async def list_triage_runs():
    base = _triage_base_dir()
    if not base.exists():
        return {"items": [], "total": 0}

    runs: list[dict[str, Any]] = []
    for state_path in base.rglob("00-state.json"):
        run_dir = state_path.parent
        try:
            runs.append(_triage_run_to_dict(run_dir, include_detail=False))
        except Exception as e:
            logger.warning("Failed to load triage run {}: {}", run_dir, e)

    runs.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
    return {"items": runs, "total": len(runs)}


@router.get("/triage/runs/{run_slug:path}")
async def get_triage_run_detail(run_slug: str):
    run_dir = _validate_triage_run_path(run_slug)
    state_path = run_dir / "00-state.json"
    if not state_path.exists():
        raise HTTPException(status_code=404, detail="triage run not found")
    return _triage_run_to_dict(run_dir, include_detail=True)


# ---------- Memory Files ----------

@router.get("/memory/entries")
async def list_memory_entries_endpoint(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    project_name: Optional[str] = None,
    scope: Optional[str] = None,
    category: Optional[str] = None,
    q: Optional[str] = None,
    db: AsyncSession = Depends(get_session),
):
    await ensure_memory_bootstrap(db)
    items, total = await list_memory_entries(
        db,
        page=page,
        page_size=page_size,
        project_name=project_name,
        scope=scope,
        category=category,
        q=q,
    )
    return {
        "items": [memory_entry_to_dict(item) for item in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/memory/entries/{entry_id}")
async def get_memory_entry_endpoint(entry_id: int, db: AsyncSession = Depends(get_session)):
    await ensure_memory_bootstrap(db)
    entry = await get_memory_entry(db, entry_id)
    if not entry:
        raise HTTPException(status_code=404, detail="memory entry not found")
    return memory_entry_to_dict(entry)


@router.post("/memory/entries")
async def create_memory_entry_endpoint(
    body: MemoryEntryCreate,
    db: AsyncSession = Depends(get_session),
):
    entry = await create_memory_entry(db, body.model_dump())
    return memory_entry_to_dict(entry)


@router.put("/memory/entries/{entry_id}")
async def update_memory_entry_endpoint(
    entry_id: int,
    body: MemoryEntryUpdate,
    db: AsyncSession = Depends(get_session),
):
    entry = await get_memory_entry(db, entry_id)
    if not entry:
        raise HTTPException(status_code=404, detail="memory entry not found")
    updated = await update_structured_memory_entry(
        db,
        entry,
        body.model_dump(exclude_unset=True),
    )
    return memory_entry_to_dict(updated)


@router.delete("/memory/entries/{entry_id}")
async def delete_memory_entry_endpoint(entry_id: int, db: AsyncSession = Depends(get_session)):
    entry = await get_memory_entry(db, entry_id)
    if not entry:
        raise HTTPException(status_code=404, detail="memory entry not found")
    await delete_structured_memory_entry(db, entry)
    return {"deleted": entry_id}


@router.get("/memory/overview")
async def get_memory_overview(db: AsyncSession = Depends(get_session)):
    await ensure_memory_bootstrap(db)
    total = (await db.execute(select(func.count(MemoryEntry.id)))).scalar() or 0
    by_scope = (await db.execute(
        select(MemoryEntry.scope, func.count(MemoryEntry.id)).group_by(MemoryEntry.scope)
    )).all()
    by_category = (await db.execute(
        select(MemoryEntry.category, func.count(MemoryEntry.id)).group_by(MemoryEntry.category)
    )).all()
    by_project = (await db.execute(
        select(MemoryEntry.project_name, func.count(MemoryEntry.id))
        .where(MemoryEntry.project_name != "")
        .group_by(MemoryEntry.project_name)
        .order_by(desc(func.count(MemoryEntry.id)))
        .limit(10)
    )).all()
    return {
        "total": total,
        "by_scope": {row[0] or "general": row[1] for row in by_scope},
        "by_category": {row[0] or "note": row[1] for row in by_category},
        "by_project": [{"project_name": row[0], "count": row[1]} for row in by_project],
    }

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


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _triage_base_dir() -> Path:
    return _repo_root() / ".triage"


def _validate_triage_run_path(run_slug: str) -> Path:
    base = _triage_base_dir().resolve()
    target = (base / run_slug).resolve()
    if not str(target).startswith(str(base)):
        raise HTTPException(status_code=400, detail="invalid triage path")
    return target


def _read_json_file(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _read_text_file(path: Path, *, max_chars: int = 4000) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")[:max_chars]


def _message_media_info(message: Message) -> dict[str, Any]:
    raw = getattr(message, "media_info_json", "") or ""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _triage_slug_from_workspace(workspace: str) -> str:
    value = (workspace or "").strip()
    if not value:
        return ""
    try:
        base = _triage_base_dir().resolve()
        target = Path(value).resolve()
        if str(target).startswith(str(base)):
            return str(target.relative_to(base)).replace("\\", "/")
    except Exception:
        return ""
    return ""


def _search_run_to_dict(search_dir: Path, *, include_content: bool) -> dict[str, Any]:
    result_path = search_dir / "search_results.json"
    summary_path = search_dir / "evidence_summary.md"
    result = _read_json_file(result_path) or {}
    created_at = datetime.fromtimestamp(search_dir.stat().st_mtime).isoformat()

    payload = {
        "run_id": search_dir.name,
        "path": str(search_dir),
        "created_at": created_at,
        "hits_total": result.get("hits_total", 0),
        "hits_truncated": result.get("hits_truncated", False),
        "matched_terms": result.get("matched_terms", []),
        "unmatched_terms": result.get("unmatched_terms", []),
        "top_files": result.get("top_files", [])[:5],
        "summary_path": str(summary_path) if summary_path.exists() else "",
        "summary_preview": _read_text_file(summary_path, max_chars=1200),
        "evidence_hits": result.get("evidence_hits", [])[:8],
    }
    if include_content:
        payload["summary_content"] = _read_text_file(summary_path, max_chars=12000)
        payload["result"] = result
    return payload


def _collect_search_runs(run_dir: Path, *, include_content: bool) -> list[dict[str, Any]]:
    search_root = run_dir / "search-runs"
    if not search_root.exists():
        return []

    items = []
    for child in search_root.iterdir():
        if child.is_dir():
            try:
                items.append(_search_run_to_dict(child, include_content=include_content))
            except Exception as e:
                logger.warning("Failed to load triage search run {}: {}", child, e)
    items.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    return items


def _read_decision_artifact(path: Path) -> dict[str, Any] | None:
    payload = _read_json_file(path)
    if payload is None:
        return None
    return {
        "path": str(path.resolve()),
        "payload": payload,
    }


def _read_analysis_trace(run_dir: Path, *, include_content: bool) -> dict[str, Any] | None:
    process_dir = run_dir / "02-process"
    trace_json_path = process_dir / "analysis_trace.json"
    trace_md_path = process_dir / "analysis_trace.md"
    trace_json = _read_json_file(trace_json_path)
    markdown_preview = _read_text_file(trace_md_path, max_chars=3000)

    if not trace_json and not markdown_preview:
        return None

    payload: dict[str, Any] = {
        "path": str(trace_json_path.resolve()) if trace_json_path.exists() else "",
        "markdown_path": str(trace_md_path.resolve()) if trace_md_path.exists() else "",
        "markdown_preview": markdown_preview,
        "runtime": (trace_json or {}).get("runtime", ""),
        "rollout_path": (trace_json or {}).get("rollout_path", ""),
        "steps": (trace_json or {}).get("steps", [])[:16],
    }
    if include_content:
        payload["markdown_content"] = _read_text_file(trace_md_path, max_chars=12000)
        payload["trace"] = trace_json or {}
    return payload


def _triage_run_to_dict(run_dir: Path, *, include_detail: bool) -> dict[str, Any]:
    state_path = run_dir / "00-state.json"
    state = _read_json_file(state_path)
    if not state:
        raise FileNotFoundError(state_path)

    base = _triage_base_dir().resolve()
    slug = str(run_dir.resolve().relative_to(base)).replace("\\", "/")
    search_runs = _collect_search_runs(run_dir, include_content=include_detail)
    latest_search = search_runs[0] if search_runs else None
    routing_decision = _read_decision_artifact(run_dir / "02-process" / "routing_decision.json")
    final_decision = _read_decision_artifact(run_dir / "02-process" / "final_decision.json")
    analysis_trace = _read_analysis_trace(run_dir, include_content=include_detail)

    payload: dict[str, Any] = {
        "slug": slug,
        "triage_dir": str(run_dir.resolve()),
        "state_path": str(state_path.resolve()),
        "project": state.get("project", ""),
        "problem_summary": state.get("problem_summary", ""),
        "phase": state.get("phase", ""),
        "mode": state.get("mode", ""),
        "confidence": state.get("confidence", ""),
        "artifact_status": (state.get("artifact_completeness") or {}).get("status", ""),
        "search_status": state.get("search_status", ""),
        "evidence_chain_status": state.get("evidence_chain_status", ""),
        "updated_at": state.get("updated_at") or state.get("created_at"),
        "created_at": state.get("created_at"),
        "missing_items": state.get("missing_items", []),
        "module_hypothesis": state.get("module_hypothesis", []),
        "target_log_files": state.get("target_log_files", []),
        "latest_search": latest_search,
        "has_process_trace": bool(routing_decision or final_decision or analysis_trace),
        "route_mode": ((routing_decision or {}).get("payload") or {}).get("route_mode", ""),
        "final_action": ((final_decision or {}).get("payload") or {}).get("action", ""),
    }

    if include_detail:
        payload["state"] = state
        payload["search_runs"] = search_runs
        payload["routing_decision"] = routing_decision
        payload["final_decision"] = final_decision
        payload["analysis_trace"] = analysis_trace
    return payload


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
            "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
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


@router.get("/messages/{message_id}/attachment")
async def proxy_message_attachment(message_id: int, db: AsyncSession = Depends(get_session)):
    msg = await db.get(Message, message_id)
    if not msg:
        raise HTTPException(status_code=404, detail="message not found")

    media_info = _message_media_info(msg)
    local_path_value = str(media_info.get("local_path") or getattr(msg, "attachment_path", "") or "").strip()
    if not local_path_value:
        raise HTTPException(status_code=404, detail="attachment not found")
    local_path = Path(local_path_value).resolve()
    if not local_path.exists():
        raise HTTPException(status_code=404, detail="attachment not found")

    media_type = str(media_info.get("mime_type") or "").strip() or "application/octet-stream"
    file_name = local_path.name
    headers = {"Content-Disposition": f'inline; filename="{file_name}"'}
    return StreamingResponse(iter([local_path.read_bytes()]), media_type=media_type, headers=headers)

def _message_to_dict(m: Message) -> dict:
    media_info = _message_media_info(m)
    return {
        "id": m.id,
        "platform": m.platform,
        "platform_message_id": m.platform_message_id,
        "chat_id": m.chat_id,
        "sender_id": m.sender_id,
        "sender_name": m.sender_name,
        "message_type": m.message_type,
        "content": m.content,
        "raw_payload": m.raw_payload,
        "media_info": media_info,
        "attachment_path": getattr(m, "attachment_path", ""),
        "classified_type": m.classified_type,
        "session_id": m.session_id,
        "pipeline_status": m.pipeline_status,
        "pipeline_error": m.pipeline_error,
        "processed_at": m.processed_at.isoformat() if m.processed_at else None,
        "sent_at": m.sent_at.isoformat() if m.sent_at else None,
        "received_at": m.received_at.isoformat() if m.received_at else None,
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }


@router.get("/feishu-images/{image_key}")
async def proxy_feishu_image(image_key: str):
    """Proxy Feishu image bytes for admin UI previews."""
    from core.connectors.feishu import FeishuClient

    client = FeishuClient()
    result = client.get_image_bytes(image_key)
    if not result:
        raise HTTPException(status_code=404, detail="image not found")

    content, file_name = result
    media_type = "image/png"
    if file_name and "." in file_name:
        ext = file_name.rsplit(".", 1)[-1].lower()
        media_type = {
            "png": "image/png",
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "gif": "image/gif",
            "webp": "image/webp",
        }.get(ext, media_type)

    return StreamingResponse(iter([content]), media_type=media_type)


def _session_to_dict(s: Session) -> dict:
    analysis_workspace = getattr(s, "analysis_workspace", "") or ""
    return {
        "id": s.id,
        "session_key": s.session_key,
        "source_platform": s.source_platform,
        "source_chat_id": s.source_chat_id,
        "owner_user_id": s.owner_user_id,
        "title": s.title,
        "topic": s.topic,
        "project": s.project,
        "agent_session_id": s.agent_session_id,
        "agent_runtime": s.agent_runtime,
        "analysis_mode": getattr(s, "analysis_mode", False),
        "analysis_workspace": analysis_workspace,
        "triage_slug": _triage_slug_from_workspace(analysis_workspace),
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
