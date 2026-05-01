from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aiosqlite
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel

import models.db  # noqa: F401


async def create_schema(db_file: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    await engine.dispose()


async def insert_message(
    db_file: Path,
    *,
    platform_message_id: str = "om_test_001",
    chat_id: str = "oc_test",
    sender_id: str = "ou_test",
    message_type: str = "text",
    content: str = "hello",
    thread_id: str = "",
    raw_payload: dict[str, Any] | None = None,
    media_info: dict[str, Any] | None = None,
    attachment_path: str = "",
) -> int:
    now = "2026-04-30T10:00:00"
    async with aiosqlite.connect(db_file) as db:
        cursor = await db.execute(
            "INSERT INTO messages (platform, platform_message_id, chat_id, sender_id, "
            "sender_name, message_type, content, received_at, raw_payload, media_info_json, "
            "attachment_path, thread_id, root_id, parent_id, pipeline_status, pipeline_error, "
            "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "feishu",
                platform_message_id,
                chat_id,
                sender_id,
                "tester",
                message_type,
                content,
                now,
                json.dumps(raw_payload or {}, ensure_ascii=False),
                json.dumps(media_info or {}, ensure_ascii=False),
                attachment_path,
                thread_id,
                "",
                "",
                "pending",
                "",
                now,
            ),
        )
        await db.commit()
        return int(cursor.lastrowid)


async def fetch_one(db_file: Path, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    async with aiosqlite.connect(db_file) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(sql, params)
        row = await cursor.fetchone()
        return dict(row) if row else None


async def fetch_all(db_file: Path, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    async with aiosqlite.connect(db_file) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(sql, params)
        return [dict(row) for row in await cursor.fetchall()]
