from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel

import models.db  # noqa: F401
from core.memory.consolidator import _infer_project_version, consolidate_memories
from models.db import MemoryEntry, Message, Session, SessionMessage


@pytest.fixture
async def db_factory(tmp_path):
    db_file = tmp_path / "memory.sqlite"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_consolidator_processes_active_project_session_incrementally(monkeypatch, db_factory, tmp_path):
    now = datetime.now()
    session_id = None

    async with db_factory() as db:
        session = Session(
            session_key="feishu_test_memory",
            source_chat_id="chat-memory",
            owner_user_id="u1",
            title="什么是fms",
            topic="fms 简介",
            project="fms-java",
            status="open",
            last_active_at=now,
            message_count=2,
            updated_at=now,
        )
        db.add(session)
        await db.commit()
        await db.refresh(session)
        session_id = session.id

        user_msg = Message(
            platform_message_id="m1",
            chat_id="chat-memory",
            sender_id="u1",
            sender_name="Tester",
            content="什么是fms？",
            classified_type="work_question",
            session_id=session.id,
            created_at=now,
        )
        bot_msg = Message(
            platform_message_id="reply_m1",
            chat_id="chat-memory",
            sender_id="bot",
            sender_name="WorkAgent",
            content="FMS 是车队管理系统。",
            classified_type="bot_reply",
            session_id=session.id,
            created_at=now,
        )
        db.add(user_msg)
        db.add(bot_msg)
        await db.commit()
        await db.refresh(user_msg)
        await db.refresh(bot_msg)

        db.add_all([
            SessionMessage(session_id=session.id, message_id=user_msg.id, role="user", sequence_no=1, created_at=now),
            SessionMessage(session_id=session.id, message_id=bot_msg.id, role="assistant", sequence_no=2, created_at=now),
        ])
        await db.commit()

    legacy_dir = tmp_path / "legacy"
    marker_dir = tmp_path / "markers"

    monkeypatch.setattr("core.memory.consolidator.async_session_factory", db_factory)
    monkeypatch.setattr(
        "core.memory.consolidator._legacy_export_path",
        lambda scope, project, title: legacy_dir / f"{project or title}.md",
    )
    monkeypatch.setattr(
        "core.memory.consolidator._snapshot_marker_path",
        lambda session_id: marker_dir / f"{session_id}.json",
    )
    monkeypatch.setattr(
        "core.memory.consolidator._extract_knowledge",
        _fake_extract_knowledge,
    )

    first = await consolidate_memories()
    assert first["sessions"] == 1
    assert first["created"] == 1

    async with db_factory() as db:
        rows = (await db.execute(select(MemoryEntry))).scalars().all()
        assert len(rows) == 1
        assert rows[0].project_name == "fms-java"
        assert rows[0].source_session_id == session_id

    second = await consolidate_memories()
    assert second["sessions"] == 0
    assert second["consolidated"] == 0


@pytest.mark.asyncio
async def test_consolidator_uses_bot_replies_from_messages_table(monkeypatch, db_factory, tmp_path):
    now = datetime.now()
    captured: list[dict] = []

    async with db_factory() as db:
        session = Session(
            session_key="feishu_test_memory_2",
            source_chat_id="chat-memory-2",
            owner_user_id="u1",
            title="动作四元组",
            topic="动作四元组",
            project="fms-java",
            status="open",
            last_active_at=now,
            message_count=2,
            updated_at=now,
        )
        db.add(session)
        await db.commit()
        await db.refresh(session)

        db.add_all([
            Message(
                platform_message_id="m2",
                chat_id="chat-memory-2",
                sender_id="u1",
                sender_name="Tester",
                content="fms 支持动作四元组吗？",
                classified_type="work_question",
                session_id=session.id,
                created_at=now,
            ),
            Message(
                platform_message_id="reply_m2",
                chat_id="chat-memory-2",
                sender_id="bot",
                sender_name="WorkAgent",
                content="支持，但要分链路看，不是所有入口都完整支持。",
                classified_type="bot_reply",
                session_id=session.id,
                created_at=now,
            ),
        ])
        await db.commit()

    monkeypatch.setattr("core.memory.consolidator.async_session_factory", db_factory)
    monkeypatch.setattr(
        "core.memory.consolidator._legacy_export_path",
        lambda scope, project, title: tmp_path / "legacy2" / f"{project or title}.md",
    )
    monkeypatch.setattr(
        "core.memory.consolidator._snapshot_marker_path",
        lambda session_id: tmp_path / "markers2" / f"{session_id}.json",
    )

    async def _capture_extract(session_data: list[dict]) -> list[dict]:
        captured.extend(session_data)
        return [{
            "source_session_id": session_data[0]["id"],
            "scope": "project",
            "project_name": session_data[0]["project"],
            "category": "solution",
            "title": "动作四元组支持情况",
            "content": "动作四元组支持情况已确认。",
            "happened_at": session_data[0]["last_active_at"],
            "tags": ["action"],
            "importance": 4,
        }]

    monkeypatch.setattr("core.memory.consolidator._extract_knowledge", _capture_extract)

    result = await consolidate_memories()
    assert result["sessions"] == 1
    assert captured, "expected consolidator to collect session context"
    assert "fms 支持动作四元组吗" in captured[0]["summary"]
    assert "支持，但要分链路看" in captured[0]["summary"]


async def _fake_extract_knowledge(session_data: list[dict]) -> list[dict]:
    item = session_data[0]
    return [{
        "source_session_id": item["id"],
        "scope": "project",
        "project_name": item["project"],
        "category": "fact",
        "title": "FMS 定义",
        "content": "FMS 是车队管理系统，属于项目长期背景知识。",
        "happened_at": item["last_active_at"],
        "tags": ["fms", "definition"],
        "importance": 3,
    }]


def test_infer_project_version_from_registered_project_description():
    inferred = _infer_project_version({
        "project": "fms-java",
        "summary": "用户在咨询 FMS 的历史实现。",
    })
    assert inferred == "1.0"
