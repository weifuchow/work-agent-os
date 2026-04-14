"""Project-centric analytics for the admin dashboard."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.projects import get_project_git_meta, get_projects
from models.db import MemoryEntry, Message, Session

PERSONAL_BUCKET = "__personal__"


async def get_project_insights(db: AsyncSession, days: int = 30) -> dict[str, Any]:
    now = datetime.now()
    cutoff = now - timedelta(days=max(1, min(days, 365)))
    registered_projects = {project.name: project for project in get_projects()}

    sessions = (await db.execute(
        select(Session)
        .where(Session.last_active_at >= cutoff)
        .order_by(Session.last_active_at.desc())
    )).scalars().all()

    message_rows = (await db.execute(
        select(
            func.coalesce(Session.project, ""),
            Message.classified_type,
            func.count(Message.id),
        )
        .join(Session, Message.session_id == Session.id, isouter=True)
        .where(Message.sender_id != "bot", Message.created_at >= cutoff)
        .group_by(func.coalesce(Session.project, ""), Message.classified_type)
    )).all()

    memory_entries = (await db.execute(
        select(MemoryEntry).order_by(MemoryEntry.updated_at.desc(), MemoryEntry.id.desc())
    )).scalars().all()

    project_metrics: dict[str, dict[str, Any]] = {
        name: _init_project_payload(name, registered_projects[name].description, registered_projects[name].path.exists())
        for name in registered_projects
    }
    personal = _init_personal_payload()

    for session in sessions:
        bucket = _bucket_name(session.project)
        target = personal if bucket == PERSONAL_BUCKET else project_metrics.setdefault(
            bucket,
            _init_project_payload(bucket, "", False),
        )
        _apply_session(target, session, now)

    for project_name, classified_type, count in message_rows:
        bucket = _bucket_name(project_name)
        target = personal if bucket == PERSONAL_BUCKET else project_metrics.setdefault(
            bucket,
            _init_project_payload(bucket, "", False),
        )
        key = classified_type or "unclassified"
        target["classification"][key] = target["classification"].get(key, 0) + count

    memory_totals = {
        "all": len(memory_entries),
        "project": 0,
        "personal": 0,
        "people": 0,
        "general": 0,
    }

    for entry in memory_entries:
        scope = entry.scope or "general"
        if scope in memory_totals:
            memory_totals[scope] += 1

        bucket = _bucket_name(entry.project_name if entry.scope == "project" else "")
        if entry.scope == "project" and bucket != PERSONAL_BUCKET:
            target = project_metrics.setdefault(bucket, _init_project_payload(bucket, "", False))
            target["memory_count"] += 1
            target["memory_by_category"][entry.category] = target["memory_by_category"].get(entry.category, 0) + 1
            if len(target["memory_highlights"]) < 3:
                target["memory_highlights"].append({
                    "id": entry.id,
                    "title": entry.title,
                    "category": entry.category,
                    "updated_at": _iso(entry.updated_at),
                })
        elif entry.scope == "personal":
            personal["memory_count"] += 1
            if len(personal["preferences"]) < 8 and entry.category in {"preference", "fact", "note"}:
                personal["preferences"].append({
                    "id": entry.id,
                    "title": entry.title,
                    "category": entry.category,
                    "content": entry.content[:180],
                    "updated_at": _iso(entry.updated_at),
                })

    hot_topics = _build_hot_topics(project_metrics)

    projects_payload = []
    total_project_sessions = 0
    active_project_sessions = 0
    for name, payload in project_metrics.items():
        payload["top_topics"] = _counter_to_list(payload.pop("_topic_counter"))
        payload["recent_sessions"] = payload["recent_sessions"][:5]
        payload["classification"] = dict(sorted(
            payload["classification"].items(),
            key=lambda item: (-item[1], item[0]),
        ))
        payload["memory_by_category"] = dict(sorted(
            payload["memory_by_category"].items(),
            key=lambda item: (-item[1], item[0]),
        ))
        total_project_sessions += payload["session_count"]
        active_project_sessions += payload["open_sessions"]
        projects_payload.append(payload)

    personal["top_topics"] = _counter_to_list(personal.pop("_topic_counter"))
    personal["recent_sessions"] = personal["recent_sessions"][:5]
    personal["classification"] = dict(sorted(
        personal["classification"].items(),
        key=lambda item: (-item[1], item[0]),
    ))

    projects_payload.sort(
        key=lambda item: (item["session_count"], item["last_activity_at"] or ""),
        reverse=True,
    )

    return {
        "period_days": days,
        "generated_at": now.isoformat(),
        "overview": {
            "registered_projects": len(registered_projects),
            "tracked_projects": len(projects_payload),
            "project_sessions": total_project_sessions,
            "active_project_sessions": active_project_sessions,
            "personal_sessions": personal["session_count"],
            "structured_memories": memory_totals["all"],
            "project_memories": memory_totals["project"],
            "personal_memories": memory_totals["personal"],
        },
        "projects": projects_payload,
        "personal": personal,
        "hot_topics": hot_topics[:10],
    }


async def build_project_summary_context(
    db: AsyncSession,
    project_name: str,
    *,
    days: int = 30,
    max_sessions: int = 12,
) -> dict[str, Any]:
    insights = await get_project_insights(db, days=days)
    bucket = _bucket_name(project_name)

    if bucket == PERSONAL_BUCKET:
        detail = insights["personal"]
    else:
        detail = next((item for item in insights["projects"] if item["name"] == bucket), None)
        if not detail:
            detail = _init_project_payload(bucket, "", False)
            detail["top_topics"] = []
            detail["recent_sessions"] = []

    cutoff = datetime.now() - timedelta(days=max(1, min(days, 365)))
    stmt = select(Session).where(Session.last_active_at >= cutoff)
    if bucket == PERSONAL_BUCKET:
        stmt = stmt.where(Session.project == "")
    else:
        stmt = stmt.where(Session.project == bucket)
    stmt = stmt.order_by(Session.last_active_at.desc()).limit(max_sessions)
    sessions = (await db.execute(stmt)).scalars().all()

    session_briefs = []
    for session in sessions:
        session_briefs.append({
            "id": session.id,
            "title": session.title,
            "topic": session.topic,
            "status": session.status,
            "message_count": session.message_count,
            "last_active_at": _iso(session.last_active_at),
            "summary": _read_summary(session.summary_path),
        })

    return {
        "period_days": days,
        "project_name": project_name,
        "bucket": bucket,
        "detail": detail,
        "hot_topics": insights["hot_topics"],
        "sessions": session_briefs,
    }


def _init_project_payload(name: str, description: str, path_exists: bool) -> dict[str, Any]:
    git_meta = get_project_git_meta(name=name) if path_exists else None
    return {
        "name": name,
        "description": description,
        "path_exists": path_exists,
        "git_version": git_meta.version_hint if git_meta else "",
        "git_branch": git_meta.branch if git_meta else "",
        "git_commit_sha": git_meta.commit_sha if git_meta else "",
        "git_commit_time": _iso(git_meta.commit_time) if git_meta and git_meta.commit_time else None,
        "session_count": 0,
        "message_count": 0,
        "open_sessions": 0,
        "active_recent_sessions": 0,
        "memory_count": 0,
        "classification": {},
        "memory_by_category": {},
        "memory_highlights": [],
        "recent_sessions": [],
        "last_activity_at": None,
        "_topic_counter": Counter(),
    }


def _init_personal_payload() -> dict[str, Any]:
    return {
        "name": PERSONAL_BUCKET,
        "label": "个人偏好 / 闲聊",
        "session_count": 0,
        "message_count": 0,
        "open_sessions": 0,
        "active_recent_sessions": 0,
        "memory_count": 0,
        "classification": {},
        "preferences": [],
        "recent_sessions": [],
        "last_activity_at": None,
        "_topic_counter": Counter(),
    }


def _apply_session(target: dict[str, Any], session: Session, now: datetime) -> None:
    target["session_count"] += 1
    target["message_count"] += session.message_count
    if session.status in {"open", "waiting"}:
        target["open_sessions"] += 1
    if session.last_active_at and session.last_active_at >= now - timedelta(days=7):
        target["active_recent_sessions"] += 1

    topic = (session.topic or session.title or "未命名问题").strip()
    target["_topic_counter"][topic] += 1

    target["recent_sessions"].append({
        "id": session.id,
        "title": session.title,
        "topic": session.topic,
        "status": session.status,
        "message_count": session.message_count,
        "risk_level": session.risk_level,
        "last_active_at": _iso(session.last_active_at),
    })
    if session.last_active_at and (
        target["last_activity_at"] is None or session.last_active_at.isoformat() > target["last_activity_at"]
    ):
        target["last_activity_at"] = session.last_active_at.isoformat()


def _build_hot_topics(project_metrics: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    combined: list[dict[str, Any]] = []
    for name, payload in project_metrics.items():
        for topic, count in payload["_topic_counter"].items():
            combined.append({
                "project_name": name,
                "topic": topic,
                "count": count,
            })
    combined.sort(key=lambda item: (-item["count"], item["project_name"], item["topic"]))
    return combined


def _counter_to_list(counter: Counter[str], limit: int = 6) -> list[dict[str, Any]]:
    return [{"topic": topic, "count": count} for topic, count in counter.most_common(limit)]


def _bucket_name(project_name: str | None) -> str:
    normalized = (project_name or "").strip()
    return normalized or PERSONAL_BUCKET


def _read_summary(path: str) -> str | None:
    if not path:
        return None
    summary_path = Path(path)
    if not summary_path.exists():
        return None
    return summary_path.read_text(encoding="utf-8")[:800]


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None
