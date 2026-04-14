"""Structured memory storage helpers."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from models.db import MemoryCategory, MemoryEntry, MemoryScope


def parse_memory_tags(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            raw = json.loads(text)
            if isinstance(raw, list):
                return _normalize_tags(raw)
        except json.JSONDecodeError:
            return _normalize_tags(text.split(","))
    if isinstance(value, list):
        return _normalize_tags(value)
    return []


def memory_entry_to_dict(entry: MemoryEntry) -> dict[str, Any]:
    return {
        "id": entry.id,
        "scope": entry.scope,
        "project_name": entry.project_name,
        "project_version": entry.project_version,
        "project_branch": entry.project_branch,
        "project_commit_sha": entry.project_commit_sha,
        "project_commit_time": _iso(entry.project_commit_time),
        "category": entry.category,
        "title": entry.title,
        "content": entry.content,
        "tags": parse_memory_tags(entry.tags_json),
        "source_type": entry.source_type,
        "source_session_id": entry.source_session_id,
        "source_message_id": entry.source_message_id,
        "importance": entry.importance,
        "happened_at": _iso(entry.happened_at),
        "valid_until": _iso(entry.valid_until),
        "first_seen_at": _iso(entry.first_seen_at),
        "last_seen_at": _iso(entry.last_seen_at),
        "occurrence_count": entry.occurrence_count,
        "created_at": _iso(entry.created_at),
        "updated_at": _iso(entry.updated_at),
    }


async def ensure_memory_bootstrap(db: AsyncSession) -> int:
    """Import legacy markdown memory files if the structured table is empty."""
    count = (await db.execute(select(func.count(MemoryEntry.id)))).scalar() or 0
    if count:
        return 0

    base = Path(settings.memory_dir)
    if not base.exists():
        return 0

    created = 0
    for path in sorted(base.rglob("*.md")):
        rel = path.relative_to(base)
        content = path.read_text(encoding="utf-8").strip()
        if not content:
            continue

        scope, category, project_name = _legacy_memory_metadata(rel)
        now = datetime.now()
        entry = MemoryEntry(
            scope=scope,
            project_name=project_name,
            category=category,
            title=path.stem,
            content=content,
            tags_json="[]",
            source_type="legacy_file",
            first_seen_at=now,
            last_seen_at=now,
        )
        db.add(entry)
        created += 1

    if created:
        await db.commit()

    return created


async def list_memory_entries(
    db: AsyncSession,
    *,
    page: int = 1,
    page_size: int = 50,
    project_name: str | None = None,
    scope: str | None = None,
    category: str | None = None,
    q: str | None = None,
) -> tuple[list[MemoryEntry], int]:
    query = select(MemoryEntry)
    count_query = select(func.count(MemoryEntry.id))
    conditions = _memory_filters(
        project_name=project_name,
        scope=scope,
        category=category,
        q=q,
    )

    if conditions:
        for condition in conditions:
            query = query.where(condition)
            count_query = count_query.where(condition)

    total = (await db.execute(count_query)).scalar() or 0
    query = (
        query.order_by(
            MemoryEntry.happened_at.desc().nullslast(),
            MemoryEntry.updated_at.desc(),
            MemoryEntry.id.desc(),
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    items = (await db.execute(query)).scalars().all()
    return items, total


async def search_memory_entries(
    db: AsyncSession,
    *,
    limit: int = 10,
    project_name: str | None = None,
    scope: str | None = None,
    category: str | None = None,
    q: str | None = None,
) -> list[MemoryEntry]:
    query = select(MemoryEntry)
    conditions = _memory_filters(
        project_name=project_name,
        scope=scope,
        category=category,
        q=q,
    )
    if conditions:
        for condition in conditions:
            query = query.where(condition)

    query = query.order_by(
        MemoryEntry.importance.desc(),
        MemoryEntry.last_seen_at.desc().nullslast(),
        MemoryEntry.updated_at.desc(),
    ).limit(max(1, min(limit, 50)))
    return (await db.execute(query)).scalars().all()


async def get_memory_entry(db: AsyncSession, entry_id: int) -> MemoryEntry | None:
    return await db.get(MemoryEntry, entry_id)


async def create_memory_entry(db: AsyncSession, payload: dict[str, Any]) -> MemoryEntry:
    data = normalize_memory_payload(payload)
    entry = MemoryEntry(**data)
    db.add(entry)
    await db.commit()
    await db.refresh(entry)
    return entry


async def update_memory_entry(
    db: AsyncSession,
    entry: MemoryEntry,
    payload: dict[str, Any],
) -> MemoryEntry:
    data = normalize_memory_payload(payload, partial=True)
    for key, value in data.items():
        setattr(entry, key, value)
    entry.updated_at = datetime.now()
    db.add(entry)
    await db.commit()
    await db.refresh(entry)
    return entry


async def delete_memory_entry(db: AsyncSession, entry: MemoryEntry) -> None:
    await db.delete(entry)
    await db.commit()


def normalize_memory_payload(payload: dict[str, Any], *, partial: bool = False) -> dict[str, Any]:
    data: dict[str, Any] = {}

    if "scope" in payload or not partial:
        scope = (payload.get("scope") or MemoryScope.general).strip() or MemoryScope.general
        if scope not in {item.value for item in MemoryScope}:
            scope = MemoryScope.general
        data["scope"] = scope

    if "project_name" in payload or not partial:
        data["project_name"] = (payload.get("project_name") or "").strip()

    if "project_version" in payload or not partial:
        data["project_version"] = (payload.get("project_version") or "").strip()

    if "project_branch" in payload or not partial:
        data["project_branch"] = (payload.get("project_branch") or "").strip()

    if "project_commit_sha" in payload or not partial:
        data["project_commit_sha"] = (payload.get("project_commit_sha") or "").strip()

    if "project_commit_time" in payload or not partial:
        data["project_commit_time"] = _to_datetime(payload.get("project_commit_time"))

    if "category" in payload or not partial:
        category = (payload.get("category") or MemoryCategory.note).strip() or MemoryCategory.note
        if category not in {item.value for item in MemoryCategory}:
            category = MemoryCategory.note
        data["category"] = category

    if "title" in payload or not partial:
        data["title"] = (payload.get("title") or "").strip()

    if "content" in payload or not partial:
        data["content"] = (payload.get("content") or "").strip()

    if "tags" in payload or "tags_json" in payload or not partial:
        tags = payload.get("tags")
        if tags is None and "tags_json" in payload:
            tags = payload.get("tags_json")
        data["tags_json"] = json.dumps(parse_memory_tags(tags), ensure_ascii=False)

    if "source_type" in payload or not partial:
        data["source_type"] = (payload.get("source_type") or "manual").strip() or "manual"

    if "source_session_id" in payload or not partial:
        data["source_session_id"] = _to_int(payload.get("source_session_id"))

    if "source_message_id" in payload or not partial:
        data["source_message_id"] = _to_int(payload.get("source_message_id"))

    if "importance" in payload or not partial:
        importance = _to_int(payload.get("importance"))
        data["importance"] = min(max(importance or 3, 1), 5)

    if "happened_at" in payload or not partial:
        data["happened_at"] = _to_datetime(payload.get("happened_at"))

    if "valid_until" in payload or not partial:
        data["valid_until"] = _to_datetime(payload.get("valid_until"))

    if "first_seen_at" in payload or not partial:
        data["first_seen_at"] = _to_datetime(payload.get("first_seen_at"))

    if "last_seen_at" in payload or not partial:
        data["last_seen_at"] = _to_datetime(payload.get("last_seen_at"))

    if "occurrence_count" in payload or not partial:
        count = _to_int(payload.get("occurrence_count"))
        data["occurrence_count"] = max(count or 1, 1)

    data["updated_at"] = datetime.now()
    if not partial:
        if data.get("first_seen_at") is None:
            data["first_seen_at"] = data.get("happened_at") or datetime.now()
        if data.get("last_seen_at") is None:
            data["last_seen_at"] = data.get("happened_at") or data["first_seen_at"]
    return data


def _memory_filters(
    *,
    project_name: str | None,
    scope: str | None,
    category: str | None,
    q: str | None,
) -> list[Any]:
    conditions: list[Any] = []

    if project_name is not None:
        normalized = project_name.strip()
        if normalized:
            conditions.append(MemoryEntry.project_name == normalized)
        else:
            conditions.append(MemoryEntry.project_name == "")

    if scope:
        conditions.append(MemoryEntry.scope == scope)

    if category:
        conditions.append(MemoryEntry.category == category)

    query = (q or "").strip()
    if query:
        like = f"%{query}%"
        conditions.append(or_(
            MemoryEntry.title.ilike(like),
            MemoryEntry.content.ilike(like),
            MemoryEntry.tags_json.ilike(like),
            MemoryEntry.project_name.ilike(like),
            MemoryEntry.project_version.ilike(like),
            MemoryEntry.project_branch.ilike(like),
            MemoryEntry.project_commit_sha.ilike(like),
        ))

    return conditions


def _legacy_memory_metadata(rel_path: Path) -> tuple[str, str, str]:
    parts = rel_path.parts
    if not parts:
        return MemoryScope.general, MemoryCategory.note, ""

    head = parts[0].lower()
    stem = rel_path.stem
    if head == "projects":
        return MemoryScope.project, MemoryCategory.note, stem
    if head == "people":
        return MemoryScope.people, MemoryCategory.person, ""
    if head == "personal":
        return MemoryScope.personal, MemoryCategory.preference, ""
    return MemoryScope.general, MemoryCategory.note, ""


def _normalize_tags(values: list[Any]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for raw in values:
        text = str(raw).strip()
        if not text:
            continue
        if text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result[:20]


def _to_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            return None
    return None


def _to_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None
