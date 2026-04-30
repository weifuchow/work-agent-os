import json

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel

import models.db  # noqa: F401
from core.connectors.message_service import save_message
from models.db import AuditLog


@pytest.fixture
async def db_session(tmp_path):
    db_file = tmp_path / "message_service.sqlite"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}")

    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        yield session

    await engine.dispose()


@pytest.mark.asyncio
async def test_save_message_audit_includes_thread_fields(db_session: AsyncSession):
    msg = await save_message(db_session, {
        "platform": "feishu",
        "platform_message_id": "om_thread_audit_001",
        "chat_id": "oc_thread",
        "chat_type": "p2p",
        "sender_id": "ou_thread",
        "sender_name": "tester",
        "message_type": "text",
        "content": "继续排查",
        "thread_id": "omt_thread_001",
        "root_id": "om_root_001",
        "parent_id": "om_parent_001",
        "is_mentioned": True,
    })

    assert msg is not None

    result = await db_session.execute(
        select(AuditLog).where(
            AuditLog.event_type == "message_received",
            AuditLog.target_id == "om_thread_audit_001",
        )
    )
    audit = result.scalar_one()
    detail = json.loads(audit.detail)

    assert detail["thread_id"] == "omt_thread_001"
    assert detail["root_id"] == "om_root_001"
    assert detail["parent_id"] == "om_parent_001"
