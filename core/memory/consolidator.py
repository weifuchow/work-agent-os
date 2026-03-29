"""Memory consolidation — archive session summaries into long-term knowledge.

Periodically scans closed/archived sessions, extracts key knowledge,
and writes to data/memory/ for future context retrieval.

Flow:
1. Query DB for sessions to consolidate (NO LLM)
2. Read session summary files (NO LLM)
3. LLM extracts reusable knowledge (one call per batch)
4. Write to data/memory/projects/ and data/memory/people/ (NO LLM)
"""

import json
from datetime import datetime, timedelta
from pathlib import Path

from loguru import logger
from sqlalchemy import select, and_

from core.config import settings
from core.database import async_session_factory
from models.db import AuditLog, Session, SessionStatus


async def consolidate_memories() -> dict:
    """Scan completed sessions and extract long-term memories.

    Returns counts of sessions processed.
    """
    async with async_session_factory() as db:
        # Find sessions that are archived/closed and haven't been consolidated
        # Use summary_path as indicator — if summary exists but no memory marker
        stmt = select(Session).where(and_(
            Session.status.in_([SessionStatus.archived, SessionStatus.closed]),
            Session.summary_path != "",
            Session.message_count >= 3,  # only meaningful sessions
        )).order_by(Session.updated_at.desc()).limit(20)

        sessions = (await db.execute(stmt)).scalars().all()

        if not sessions:
            return {"consolidated": 0}

        # Read summaries and batch them
        session_data = []
        for s in sessions:
            marker = _memory_marker_path(s.id)
            if marker.exists():
                continue  # already consolidated

            summary = _read_summary(s.summary_path)
            if not summary:
                continue

            session_data.append({
                "id": s.id,
                "title": s.title,
                "project": s.project,
                "topic": s.topic,
                "summary": summary[:800],
                "message_count": s.message_count,
            })

        if not session_data:
            return {"consolidated": 0}

        # LLM extracts knowledge (one batch call)
        knowledge = await _extract_knowledge(session_data)

        # Write to memory files
        count = 0
        for item in knowledge:
            project = item.get("project", "general")
            content = item.get("content", "")
            if not content:
                continue

            # Write project knowledge
            project_dir = Path(settings.memory_dir) / "projects"
            project_dir.mkdir(parents=True, exist_ok=True)
            filepath = project_dir / f"{_safe_filename(project)}.md"

            if filepath.exists():
                existing = filepath.read_text(encoding="utf-8")
                filepath.write_text(existing + "\n\n" + content, encoding="utf-8")
            else:
                filepath.write_text(content, encoding="utf-8")

            count += 1

        # Write people knowledge if extracted
        for item in knowledge:
            people = item.get("people", [])
            if not people:
                continue
            people_dir = Path(settings.memory_dir) / "people"
            people_dir.mkdir(parents=True, exist_ok=True)
            for person in people:
                name = person.get("name", "")
                info = person.get("info", "")
                if not name or not info:
                    continue
                filepath = people_dir / f"{_safe_filename(name)}.md"
                if filepath.exists():
                    existing = filepath.read_text(encoding="utf-8")
                    if info not in existing:
                        filepath.write_text(existing + "\n- " + info, encoding="utf-8")
                else:
                    filepath.write_text(f"# {name}\n\n- {info}", encoding="utf-8")

        # Mark sessions as consolidated
        for sd in session_data:
            _memory_marker_path(sd["id"]).parent.mkdir(parents=True, exist_ok=True)
            _memory_marker_path(sd["id"]).write_text(
                datetime.now().isoformat(), encoding="utf-8"
            )

        # Audit
        async with async_session_factory() as db2:
            db2.add(AuditLog(
                event_type="memory_consolidated",
                target_type="session",
                target_id=",".join(str(sd["id"]) for sd in session_data),
                detail=f"consolidated {count} knowledge items from {len(session_data)} sessions",
                operator="consolidator",
            ))
            await db2.commit()

        logger.info("Memory consolidation: {} items from {} sessions",
                     count, len(session_data))
        return {"consolidated": count, "sessions": len(session_data)}


async def _extract_knowledge(session_data: list[dict]) -> list[dict]:
    """LLM extracts reusable knowledge from session summaries. Single batch call."""
    from core.orchestrator.claude_client import claude_client

    system_prompt = """你是知识提取器。从工作会话摘要中提取可复用的长期知识。

输出 JSON 数组，每个元素：
{
  "project": "项目名",
  "content": "提取的知识（Markdown 格式，包含日期标记）",
  "people": [{"name": "人名", "info": "此人在此事中的角色/特点"}]
}

提取标准：
- 技术决策和理由
- 项目进展里程碑
- 问题和解决方案
- 人员职责和协作模式
- 不提取临时性、一次性信息

保持简洁，每条知识 2-3 句话。"""

    context = "以下是需要提取知识的会话摘要：\n\n"
    for sd in session_data:
        context += f"--- 会话 #{sd['id']}: {sd['title']} (项目: {sd['project']}) ---\n{sd['summary']}\n\n"

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
        # Fallback: just store raw summaries
        return [
            {"project": sd.get("project", "general"),
             "content": f"## {sd['title']} ({datetime.now().strftime('%Y-%m-%d')})\n{sd['summary'][:300]}",
             "people": []}
            for sd in session_data
        ]


def _read_summary(path: str) -> str | None:
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        return None
    return p.read_text(encoding="utf-8")


def _memory_marker_path(session_id: int) -> Path:
    return Path(settings.sessions_dir) / str(session_id) / ".consolidated"


def _safe_filename(name: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
    return safe[:64] or "general"
