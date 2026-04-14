from datetime import datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel

import models.db  # noqa: F401
from core.analytics.projects import get_project_insights
from core.memory.store import create_memory_entry, delete_memory_entry, list_memory_entries, update_memory_entry
from models.db import MemoryEntry, Message, Session


@pytest.fixture
async def db_session(tmp_path):
    db_file = tmp_path / "analytics.sqlite"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}")

    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session

    await engine.dispose()


@pytest.mark.asyncio
async def test_project_insights_aggregates_project_and_personal_data(db_session: AsyncSession):
    now = datetime.now()

    project_session = Session(
        session_key="project-session",
        source_chat_id="chat-project",
        owner_user_id="u1",
        title="排查路由异常",
        topic="路由异常",
        project="work-agent-os",
        status="open",
        last_active_at=now,
        message_count=4,
    )
    personal_session = Session(
        session_key="personal-session",
        source_chat_id="chat-personal",
        owner_user_id="u1",
        title="喜欢简短回复",
        topic="回复风格",
        project="",
        status="waiting",
        last_active_at=now - timedelta(hours=2),
        message_count=2,
    )
    db_session.add(project_session)
    db_session.add(personal_session)
    await db_session.commit()
    await db_session.refresh(project_session)
    await db_session.refresh(personal_session)

    db_session.add_all([
        Message(
            platform_message_id="m1",
            chat_id="chat-project",
            sender_id="u1",
            sender_name="Tester",
            content="项目路由有问题",
            classified_type="work_question",
            session_id=project_session.id,
            created_at=now,
        ),
        Message(
            platform_message_id="m2",
            chat_id="chat-personal",
            sender_id="u1",
            sender_name="Tester",
            content="以后回复简短点",
            classified_type="chat",
            session_id=personal_session.id,
            created_at=now,
        ),
    ])
    await db_session.commit()

    await create_memory_entry(db_session, {
        "scope": "project",
        "project_name": "work-agent-os",
        "project_version": "0.1.0",
        "category": "issue",
        "title": "路由异常排查经验",
        "content": "路由异常通常先检查 session 绑定和 thread_id。",
        "tags": ["routing", "session"],
    })
    await create_memory_entry(db_session, {
        "scope": "personal",
        "project_name": "",
        "category": "preference",
        "title": "回复风格",
        "content": "用户偏好简短直接的回复。",
        "tags": ["style"],
    })

    insights = await get_project_insights(db_session, days=30)

    project = next(item for item in insights["projects"] if item["name"] == "work-agent-os")
    assert project["session_count"] == 1
    assert project["memory_count"] == 1
    assert project["classification"]["work_question"] == 1
    assert project["top_topics"][0]["topic"] == "路由异常"

    assert insights["personal"]["session_count"] == 1
    assert insights["personal"]["memory_count"] == 1
    assert insights["personal"]["classification"]["chat"] == 1


@pytest.mark.asyncio
async def test_structured_memory_crud_helpers(db_session: AsyncSession):
    created = await create_memory_entry(db_session, {
        "scope": "project",
        "project_name": "work-agent-os",
        "project_version": "0.1.0",
        "category": "decision",
        "title": "统一记忆结构",
        "content": "记忆统一进入 memory_entries 表。",
        "tags": ["memory", "schema"],
    })
    assert created.id is not None

    items, total = await list_memory_entries(db_session, project_name="work-agent-os")
    assert total == 1
    assert items[0].title == "统一记忆结构"
    assert items[0].project_version == "0.1.0"

    updated = await update_memory_entry(db_session, created, {
        "category": "solution",
        "project_version": "0.1.1",
        "occurrence_count": 3,
    })
    assert updated.category == "solution"
    assert updated.project_version == "0.1.1"
    assert updated.occurrence_count == 3

    loaded = await db_session.get(MemoryEntry, created.id)
    assert loaded is not None

    await delete_memory_entry(db_session, loaded)
    items_after_delete, total_after_delete = await list_memory_entries(db_session, project_name="work-agent-os")
    assert total_after_delete == 0
    assert items_after_delete == []
