"""Memory consolidation — incrementally extract structured long-term memories."""

import json
import hashlib
import re
from datetime import datetime
from pathlib import Path

from loguru import logger
from sqlalchemy import select

from core.config import settings
from core.projects import get_project, get_project_git_meta
from core.memory.store import normalize_memory_payload
from core.database import async_session_factory
from models.db import (
    AuditLog,
    MemoryCategory,
    MemoryEntry,
    MemoryScope,
    Message,
    Session,
)

PROJECT_MIN_MESSAGES = 2
PERSONAL_MIN_MESSAGES = 3
RECENT_MESSAGE_LIMIT = 12
SNAPSHOT_SCHEMA_VERSION = 8


async def consolidate_memories() -> dict:
    """Incrementally extract long-term memories from eligible sessions.

    Works for active sessions too. A snapshot marker prevents reprocessing
    unchanged sessions.
    """
    async with async_session_factory() as db:
        stmt = (
            select(Session)
            .where(Session.message_count >= PROJECT_MIN_MESSAGES)
            .order_by(Session.updated_at.desc())
            .limit(20)
        )

        sessions = (await db.execute(stmt)).scalars().all()

        if not sessions:
            return {"consolidated": 0, "sessions": 0}

        session_data = []
        for s in sessions:
            if not _is_memory_candidate(s):
                continue

            context_type, summary = await _load_session_context(db, s)
            if not summary:
                continue

            digest = _content_digest(summary)
            if _is_snapshot_unchanged(s, digest):
                continue

            git_meta = get_project_git_meta(name=s.project) if s.project else None
            session_data.append({
                "id": s.id,
                "title": s.title,
                "project": s.project,
                "topic": s.topic,
                "project_version": git_meta.version_hint if git_meta else "",
                "project_branch": git_meta.branch if git_meta else "",
                "project_commit_sha": git_meta.commit_sha if git_meta else "",
                "project_commit_time": git_meta.commit_time.isoformat() if git_meta and git_meta.commit_time else None,
                "summary": summary[:800],
                "context_type": context_type,
                "message_count": s.message_count,
                "status": s.status,
                "last_active_at": s.last_active_at.isoformat() if s.last_active_at else None,
                "digest": digest,
            })

        if not session_data:
            return {"consolidated": 0, "sessions": 0}

        # LLM extracts structured knowledge (one batch call)
        knowledge = await _extract_knowledge(session_data)
        session_lookup = {item["id"]: item for item in session_data}

        # Persist structured memory entries
        created_entries: list[MemoryEntry] = []
        refreshed_entries = 0
        async with async_session_factory() as db2:
            for item in knowledge:
                source_session = _resolve_source_session(item, session_data, session_lookup)
                payload = normalize_memory_payload({
                    "scope": item.get("scope") or _default_scope(item),
                    "project_name": (
                        (source_session.get("project") if source_session else "")
                        or item.get("project_name")
                        or item.get("project")
                        or ""
                    ),
                    "project_version": (
                        (source_session.get("project_version") if source_session else "")
                        or item.get("project_version")
                        or (_infer_project_version(source_session) if source_session else "")
                    ),
                    "project_branch": (
                        (source_session.get("project_branch") if source_session else "")
                        or item.get("project_branch")
                        or ""
                    ),
                    "project_commit_sha": (
                        (source_session.get("project_commit_sha") if source_session else "")
                        or item.get("project_commit_sha")
                        or ""
                    ),
                    "project_commit_time": (
                        (source_session.get("project_commit_time") if source_session else None)
                        or item.get("project_commit_time")
                    ),
                    "category": item.get("category") or MemoryCategory.note,
                    "title": item.get("title") or "会话记忆",
                    "content": item.get("content") or "",
                    "tags": item.get("tags") or [],
                    "source_type": item.get("source_type") or "session_summary",
                    "source_session_id": (source_session.get("id") if source_session else item.get("source_session_id")),
                    "importance": item.get("importance") or 3,
                    "happened_at": item.get("happened_at"),
                    "first_seen_at": item.get("first_seen_at") or item.get("happened_at"),
                    "last_seen_at": item.get("last_seen_at") or item.get("happened_at"),
                    "occurrence_count": item.get("occurrence_count") or 1,
                })
                if not payload.get("content"):
                    continue
                existing_stmt = select(MemoryEntry).where(
                    MemoryEntry.source_session_id == payload.get("source_session_id"),
                    MemoryEntry.scope == payload.get("scope"),
                    MemoryEntry.title == payload.get("title"),
                ).limit(1)
                existing = (await db2.execute(existing_stmt)).scalar_one_or_none()
                if existing:
                    existing.project_name = payload.get("project_name", existing.project_name)
                    existing.project_version = payload.get("project_version", existing.project_version)
                    existing.project_branch = payload.get("project_branch", existing.project_branch)
                    existing.project_commit_sha = payload.get("project_commit_sha", existing.project_commit_sha)
                    existing.project_commit_time = payload.get("project_commit_time") or existing.project_commit_time
                    existing.category = payload.get("category", existing.category)
                    existing.content = payload.get("content", existing.content)
                    existing.tags_json = payload.get("tags_json", existing.tags_json)
                    existing.source_type = payload.get("source_type", existing.source_type)
                    existing.importance = max(existing.importance, payload.get("importance", existing.importance))
                    existing.happened_at = payload.get("happened_at") or existing.happened_at
                    existing.last_seen_at = payload.get("last_seen_at") or datetime.now()
                    existing.updated_at = datetime.now()
                    db2.add(existing)
                    refreshed_entries += 1
                else:
                    entry = MemoryEntry(**payload)
                    db2.add(entry)
                    created_entries.append(entry)

            if created_entries or refreshed_entries:
                await db2.commit()

            db2.add(AuditLog(
                event_type="memory_consolidated",
                target_type="session",
                target_id=",".join(str(sd["id"]) for sd in session_data),
                detail=(
                    f"created {len(created_entries)} memory items, refreshed {refreshed_entries}, "
                    f"from {len(session_data)} session snapshots"
                ),
                operator="consolidator",
            ))
            await db2.commit()

        # Legacy markdown export for compatibility
        count = 0
        for item in knowledge:
            scope = item.get("scope") or _default_scope(item)
            project = item.get("project_name") or item.get("project") or "general"
            title = item.get("title") or "会话记忆"
            content = item.get("content", "")
            if not content:
                continue

            filepath = _legacy_export_path(scope, project, title)
            filepath.parent.mkdir(parents=True, exist_ok=True)
            block = _legacy_markdown_block(item)
            if filepath.exists():
                existing = filepath.read_text(encoding="utf-8")
                filepath.write_text(existing + "\n\n" + block, encoding="utf-8")
            else:
                filepath.write_text(block, encoding="utf-8")

            count += 1

        # Mark session snapshot digests to support incremental extraction
        for sd in session_data:
            _write_snapshot_marker(
                session_id=sd["id"],
                session_updated_at=next((s.updated_at.isoformat() for s in sessions if s.id == sd["id"] and s.updated_at), ""),
                message_count=sd["message_count"],
                digest=sd["digest"],
            )

        logger.info(
            "Memory consolidation: {} created, {} refreshed from {} sessions",
            len(created_entries), refreshed_entries, len(session_data)
        )
        return {
            "consolidated": count,
            "created": len(created_entries),
            "refreshed": refreshed_entries,
            "sessions": len(session_data),
        }


async def _extract_knowledge(session_data: list[dict]) -> list[dict]:
    """LLM extracts reusable structured knowledge from session summaries."""
    from core.orchestrator.claude_client import claude_client

    system_prompt = """你是长期记忆提取器。请从工作会话摘要中提取可复用的结构化记忆。

输出必须是 JSON 数组，每个元素字段：
{
  "source_session_id": 123,
  "scope": "project | personal | people | general",
  "project_name": "项目名，没有则空字符串",
  "project_version": "项目版本号，没有则空字符串，例如 3.0、v4.8.0.6",
  "project_branch": "Git 分支名，没有则空字符串",
  "project_commit_sha": "Git commit 短 SHA，没有则空字符串",
  "project_commit_time": "Git commit 时间，ISO 日期时间，没有则 null",
  "category": "decision | milestone | issue | solution | preference | person | fact | note",
  "title": "记忆标题",
  "content": "记忆正文，2-4 句，保留关键时间/背景",
  "happened_at": "可选，ISO 日期或日期时间，例如 2026-04-13",
  "tags": ["标签1", "标签2"],
  "importance": 1-5
}

提取标准：
- 项目技术决策、进度节点、长期问题、可复用解法
- 用户个人偏好、沟通习惯、稳定背景信息
- 与项目或任务强相关的人物信息
- 不提取一次性寒暄、纯临时状态、没有长期价值的细节

规则：
- 涉及具体项目时 scope=project 并填写 project_name
- 如果上下文中出现项目版本、release、版本号或里程碑版本，尽量填 project_version
- 没有项目但属于用户长期偏好/习惯时 scope=personal
- 人物画像时 scope=people, category=person
- 若摘要里有明确日期，尽量写入 happened_at
- 输出纯 JSON，不要 markdown 代码块。"""

    context = "以下是需要提取知识的会话摘要：\n\n"
    for sd in session_data:
        context += (
            f"--- 会话 #{sd['id']}: {sd['title']} "
            f"(项目: {sd['project'] or '无'}, 主题: {sd['topic'] or '无'}, "
            f"版本: {sd.get('project_version') or '无'}, 分支: {sd.get('project_branch') or '无'}, "
            f"commit: {sd.get('project_commit_sha') or '无'}, commit时间: {sd.get('project_commit_time') or '无'}, "
            f"最近活跃: {sd['last_active_at']}, 状态: {sd['status']}, "
            f"来源: {sd['context_type']}) ---\n{sd['summary']}\n\n"
        )

    try:
        text = await claude_client.chat(
            messages=[{"role": "user", "content": context}],
            system=system_prompt,
            max_tokens=2048,
        )
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        return json.loads(text)
    except Exception as e:
        logger.warning("Knowledge extraction failed: {}", e)
        return [_build_fallback_memory_item(sd) for sd in session_data]


def _read_summary(path: str) -> str | None:
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        return None
    return p.read_text(encoding="utf-8")


def _memory_marker_path(session_id: int) -> Path:
    return Path(settings.sessions_dir) / str(session_id) / ".consolidated"


def _snapshot_marker_path(session_id: int) -> Path:
    return Path(settings.sessions_dir) / str(session_id) / ".memory_snapshot.json"


def _safe_filename(name: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
    return safe[:64] or "general"


def _default_scope(item: dict) -> str:
    if item.get("scope"):
        return item["scope"]
    if item.get("project_name") or item.get("project"):
        return MemoryScope.project
    return MemoryScope.personal


def _legacy_export_path(scope: str, project: str, title: str) -> Path:
    base = Path(settings.memory_dir)
    if scope == MemoryScope.project:
        return base / "projects" / f"{_safe_filename(project or 'general')}.md"
    if scope == MemoryScope.people:
        return base / "people" / "index.md"
    if scope == MemoryScope.personal:
        return base / "personal" / "preferences.md"
    return base / "general" / f"{_safe_filename(title)}.md"


def _legacy_markdown_block(item: dict) -> str:
    title = item.get("title") or "会话记忆"
    category = item.get("category") or "note"
    happened_at = item.get("happened_at") or datetime.now().strftime("%Y-%m-%d")
    tags = ", ".join(item.get("tags") or [])
    lines = [f"## {title}", f"- 时间: {happened_at}", f"- 类别: {category}"]
    if item.get("project_version"):
        lines.append(f"- 版本: {item['project_version']}")
    if item.get("project_branch"):
        lines.append(f"- 分支: {item['project_branch']}")
    if item.get("project_commit_sha"):
        lines.append(f"- Commit: {item['project_commit_sha']}")
    if item.get("project_commit_time"):
        lines.append(f"- 提交时间: {item['project_commit_time']}")
    if tags:
        lines.append(f"- 标签: {tags}")
    lines.append("")
    lines.append((item.get("content") or "").strip())
    return "\n".join(lines).strip()


def _resolve_source_session(
    item: dict,
    session_data: list[dict],
    session_lookup: dict[int, dict],
) -> dict | None:
    source_session_id = item.get("source_session_id")
    if source_session_id:
        try:
            resolved = session_lookup.get(int(source_session_id))
            if resolved:
                return resolved
        except (TypeError, ValueError):
            pass

    item_project = (item.get("project_name") or item.get("project") or "").strip()
    item_title = (item.get("title") or "").strip()

    if item_project and item_title:
        exact = [
            session for session in session_data
            if (session.get("project") or "").strip() == item_project
            and (session.get("title") or "").strip() == item_title
        ]
        if len(exact) == 1:
            return exact[0]

    if item_project:
        by_project = [
            session for session in session_data
            if (session.get("project") or "").strip() == item_project
        ]
        if len(by_project) == 1:
            return by_project[0]

    if len(session_data) == 1:
        return session_data[0]

    return None


def _build_fallback_memory_item(sd: dict) -> dict:
    user_lines: list[str] = []
    assistant_lines: list[str] = []
    for raw_line in (sd.get("summary") or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        speaker, content = _parse_message_line(line)
        if not content:
            continue
        if _is_assistant_speaker(speaker):
            assistant_lines.append(content)
        else:
            user_lines.append(content)

    latest_question = user_lines[-1] if user_lines else (sd.get("topic") or sd.get("title") or "会话问题")
    first_question = user_lines[0] if user_lines else latest_question
    latest_answer = assistant_lines[-1] if assistant_lines else ""

    if latest_answer:
        content = f"用户问题：{latest_question}\n当前结论：{latest_answer[:400]}"
        category = MemoryCategory.fact if sd.get("project") else MemoryCategory.note
    else:
        content = f"待跟进问题：{latest_question}"
        if len(user_lines) > 1:
            content += f"\n上下文补充：{first_question[:200]}"
        category = MemoryCategory.issue if sd.get("project") else MemoryCategory.note

    return {
        "source_session_id": sd["id"],
        "scope": MemoryScope.project if sd.get("project") else MemoryScope.personal,
        "project_name": sd.get("project", ""),
        "project_version": _infer_project_version(sd),
        "project_branch": sd.get("project_branch", ""),
        "project_commit_sha": sd.get("project_commit_sha", ""),
        "project_commit_time": sd.get("project_commit_time"),
        "category": category,
        "title": sd["title"] or sd.get("topic") or latest_question[:60],
        "content": content,
        "happened_at": sd.get("last_active_at"),
        "tags": [sd.get("project")] if sd.get("project") else ["recent-messages"],
        "importance": 3,
        "source_type": "session_summary" if sd.get("context_type") == "summary" else "recent_messages",
    }


def _is_memory_candidate(session: Session) -> bool:
    if session.project:
        return session.message_count >= PROJECT_MIN_MESSAGES
    return session.message_count >= PERSONAL_MIN_MESSAGES


async def _load_session_context(db, session: Session) -> tuple[str, str | None]:
    summary = _read_summary(session.summary_path)
    if summary:
        return "summary", summary

    stmt = (
        select(Message)
        .where(Message.session_id == session.id)
        .order_by(Message.created_at.desc())
        .limit(RECENT_MESSAGE_LIMIT)
    )
    recent_messages = (await db.execute(stmt)).scalars().all()
    if not recent_messages:
        return "recent_messages", None

    messages: list[str] = []
    for msg in reversed(recent_messages):
        sender = msg.sender_name or msg.sender_id or ("assistant" if msg.sender_id == "bot" else "user")
        content = _normalize_message_content(msg.content or "", is_bot=(msg.sender_id == "bot"))
        if not content:
            continue
        messages.append(f"[{sender}] {content[:300]}")

    if not messages:
        return "recent_messages", None

    text = "\n".join(messages)
    return "recent_messages", text[:1600]


def _content_digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _parse_message_line(line: str) -> tuple[str, str]:
    if line.startswith("[") and "]" in line:
        speaker, content = line[1:].split("]", 1)
        return speaker.strip(), content.strip()
    return "", line


def _is_assistant_speaker(speaker: str) -> bool:
    normalized = speaker.strip().lower()
    return normalized in {"workagent", "bot", "assistant"} or normalized.startswith("reply_")


def _normalize_message_content(content: str, *, is_bot: bool) -> str:
    text = " ".join(content.splitlines()).strip()
    if not text:
        return ""
    if is_bot:
        parsed = _extract_reply_content(text)
        if parsed:
            text = parsed
    return text


def _extract_reply_content(text: str) -> str | None:
    candidate = text.strip()
    if not candidate:
        return None
    try:
        data = json.loads(candidate)
        if isinstance(data, dict) and isinstance(data.get("reply_content"), str):
            return " ".join(data["reply_content"].splitlines()).strip()
    except Exception:
        return None
    return None


def _infer_project_version(sd: dict) -> str:
    existing = (sd.get("project_version") or "").strip()
    if existing:
        return existing

    summary = sd.get("summary") or ""
    project_name = sd.get("project") or ""

    explicit = _extract_version(summary)
    if explicit:
        return explicit

    if project_name:
        project = get_project(project_name)
        if project:
            inferred = _extract_version(project.description)
            if inferred:
                return inferred

    return ""


def _extract_version(text: str) -> str | None:
    if not text:
        return None

    patterns = [
        r"\b[vV](\d+(?:\.\d+){1,4})\b",
        r"(?:版本|version|release|sprint)\s*[:：]?\s*([A-Za-z]*\d+(?:\.\d+){1,4})\b",
        r"\b(?:Riot|RIOT|Allspark|FMS)\s*(\d+(?:\.\d+){1,4})\b",
    ]

    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            value = match.group(1).strip()
            if value:
                return value
    return None


def _is_snapshot_unchanged(session: Session, digest: str) -> bool:
    marker = _snapshot_marker_path(session.id)
    if not marker.exists():
        return False
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except Exception:
        return False
    session_updated = session.updated_at.isoformat() if session.updated_at else ""
    return (
        data.get("schema_version") == SNAPSHOT_SCHEMA_VERSION
        and
        data.get("session_updated_at") == session_updated
        and data.get("message_count") == session.message_count
        and data.get("digest") == digest
    )


def _write_snapshot_marker(
    *,
    session_id: int,
    session_updated_at: str,
    message_count: int,
    digest: str,
) -> None:
    marker = _snapshot_marker_path(session_id)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "session_updated_at": session_updated_at,
        "message_count": message_count,
        "digest": digest,
        "processed_at": datetime.now().isoformat(),
    }, ensure_ascii=False), encoding="utf-8")
