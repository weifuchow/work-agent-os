"""Database repositories for the core workflow."""

from __future__ import annotations

import json
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Any

import aiosqlite

from core.config import settings
from core.orchestrator.agent_runtime import DEFAULT_AGENT_RUNTIME, normalize_agent_runtime


DEFAULT_DB_PATH = settings.db_dir / "app.sqlite"


class Repository:
    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = str(db_path or DEFAULT_DB_PATH)
        self._columns_cache: dict[str, set[str]] = {}
        self._tables_cache: set[str] | None = None

    async def read_message(self, message_id: int) -> dict[str, Any] | None:
        async with self._connect() as db:
            cursor = await db.execute("SELECT * FROM messages WHERE id = ?", (message_id,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def read_session(self, session_id: int | None) -> dict[str, Any] | None:
        if not session_id:
            return None
        if not await self.table_exists("sessions"):
            return None
        async with self._connect() as db:
            cursor = await db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def update_message(self, message_id: int, **fields: Any) -> None:
        await self._update_by_id("messages", message_id, fields)

    async def reset_message_for_reprocess(self, message_id: int) -> None:
        await self.update_message(
            message_id,
            pipeline_status="pending",
            pipeline_error="",
            classified_type=None,
            session_id=None,
            processed_at=None,
        )

    async def find_open_session_by_thread(self, thread_id: str) -> dict[str, Any] | None:
        if not thread_id or not await self.table_exists("sessions"):
            return None
        columns = await self.table_columns("sessions")
        # Feishu thread_id is the strongest conversation key. Reuse it
        # regardless of local session status.
        order_by = "last_active_at DESC, id DESC" if "last_active_at" in columns else "id DESC"
        async with self._connect() as db:
            cursor = await db.execute(
                f"SELECT * FROM sessions WHERE thread_id = ? ORDER BY {order_by} LIMIT 1",
                (thread_id,),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def create_session_for_message(self, msg: dict[str, Any], now: str) -> dict[str, Any] | None:
        if not await self.table_exists("sessions"):
            return None
        import uuid

        content = str(msg.get("content") or "新会话")
        agent_runtime = await self._current_agent_runtime()
        values = {
            "session_key": (
                f"feishu_{str(msg.get('chat_id') or '')[:16]}_"
                f"{now.replace(':', '').replace('-', '')[:15]}_{uuid.uuid4().hex[:6]}"
            ),
            "source_platform": msg.get("platform") or "feishu",
            "source_chat_id": msg.get("chat_id") or "",
            "owner_user_id": msg.get("sender_id") or "",
            "title": content[:64],
            "topic": "",
            "project": "",
            "priority": "normal",
            "status": "open",
            "thread_id": msg.get("thread_id") or "",
            "agent_runtime": agent_runtime,
            "analysis_mode": False,
            "analysis_workspace": "",
            "summary_path": "",
            "last_active_at": now,
            "message_count": 0,
            "risk_level": "low",
            "needs_manual_review": False,
            "created_at": now,
            "updated_at": now,
        }
        session_id = await self._insert("sessions", values)
        return await self.read_session(session_id) if session_id else None

    async def attach_message_to_session(
        self,
        *,
        message_id: int,
        session_id: int,
        now: str,
    ) -> None:
        await self.update_message(message_id, session_id=session_id)

        session_columns = await self.table_columns("sessions")
        updates: dict[str, Any] = {}
        if "last_active_at" in session_columns:
            updates["last_active_at"] = now
        if "updated_at" in session_columns:
            updates["updated_at"] = now
        if "message_count" in session_columns:
            async with self._connect() as db:
                await db.execute(
                    "UPDATE sessions SET message_count = COALESCE(message_count, 0) + 1 "
                    "WHERE id = ?",
                    (session_id,),
                )
                await db.commit()
            updates.pop("message_count", None)
        if updates:
            await self._update_by_id("sessions", session_id, updates)

        if not await self.table_exists("session_messages"):
            return
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT 1 FROM session_messages WHERE session_id = ? AND message_id = ?",
                (session_id, message_id),
            )
            if await cursor.fetchone():
                return
            cursor = await db.execute(
                "SELECT COALESCE(MAX(sequence_no), 0) FROM session_messages WHERE session_id = ?",
                (session_id,),
            )
            max_seq = (await cursor.fetchone())[0]
            await db.execute(
                "INSERT INTO session_messages (session_id, message_id, role, sequence_no, created_at) "
                "VALUES (?,?,?,?,?)",
                (session_id, message_id, "user", max_seq + 1, now),
            )
            await db.commit()

    async def history_for_session(self, session_id: int | None) -> list[dict[str, Any]]:
        if not session_id:
            return []
        if await self.table_exists("session_messages"):
            async with self._connect() as db:
                cursor = await db.execute(
                    "SELECT m.* FROM session_messages sm "
                    "JOIN messages m ON m.id = sm.message_id "
                    "WHERE sm.session_id = ? ORDER BY sm.sequence_no ASC, sm.id ASC",
                    (session_id,),
                )
                return [dict(row) for row in await cursor.fetchall()]

        columns = await self.table_columns("messages")
        if "session_id" not in columns:
            return []
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY created_at ASC, id ASC",
                (session_id,),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def update_session_patch(
        self,
        session_id: int | None,
        patch: dict[str, Any],
        *,
        now: str,
    ) -> None:
        if not session_id or not patch or not await self.table_exists("sessions"):
            return

        columns = await self.table_columns("sessions")
        allowed = {
            "agent_session_id",
            "agent_runtime",
            "project",
            "thread_id",
            "topic",
            "title",
            "status",
            "analysis_workspace",
        }
        updates = {
            key: value
            for key, value in patch.items()
            if key in allowed and key in columns and value not in (None, "")
        }
        if "updated_at" in columns and updates:
            updates["updated_at"] = now
        await self._update_by_id("sessions", session_id, updates)

    async def save_bot_reply(
        self,
        *,
        source_message: dict[str, Any],
        session_id: int | None,
        reply_content: str,
        reply_type: str,
        raw_payload: dict[str, Any],
        now: str,
    ) -> int | None:
        platform_message_id = str(source_message.get("platform_message_id") or source_message.get("id") or "")
        reply_platform_id = f"reply_{platform_message_id}"
        existing = await self.find_message_by_platform_id(reply_platform_id)
        if existing:
            await self.update_message(
                int(existing["id"]),
                content=reply_content[:4000],
                raw_payload=json.dumps(raw_payload, ensure_ascii=False),
                processed_at=now,
                pipeline_status="completed",
            )
            return int(existing["id"])

        values = {
            "platform": source_message.get("platform") or "feishu",
            "platform_message_id": reply_platform_id,
            "chat_id": source_message.get("chat_id") or "",
            "sender_id": "bot",
            "sender_name": "WorkAgent",
            "message_type": reply_type,
            "content": reply_content[:4000],
            "received_at": now,
            "raw_payload": json.dumps(raw_payload, ensure_ascii=False),
            "media_info_json": "",
            "attachment_path": "",
            "thread_id": raw_payload.get("delivery", {}).get("thread_id") or "",
            "root_id": raw_payload.get("delivery", {}).get("root_id") or "",
            "parent_id": source_message.get("platform_message_id") or "",
            "classified_type": "bot_reply",
            "session_id": session_id,
            "pipeline_status": "completed",
            "pipeline_error": "",
            "processed_at": now,
            "created_at": now,
        }
        reply_id = await self._insert("messages", values)
        if reply_id and session_id and await self.table_exists("session_messages"):
            async with self._connect() as db:
                cursor = await db.execute(
                    "SELECT COALESCE(MAX(sequence_no), 0) FROM session_messages WHERE session_id = ?",
                    (session_id,),
                )
                max_seq = (await cursor.fetchone())[0]
                await db.execute(
                    "INSERT INTO session_messages (session_id, message_id, role, sequence_no, created_at) "
                    "VALUES (?,?,?,?,?)",
                    (session_id, reply_id, "assistant", max_seq + 1, now),
                )
                await db.commit()
        return reply_id

    async def find_message_by_platform_id(self, platform_message_id: str) -> dict[str, Any] | None:
        columns = await self.table_columns("messages")
        if "platform_message_id" not in columns:
            return None
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT * FROM messages WHERE platform_message_id = ? ORDER BY id DESC LIMIT 1",
                (platform_message_id,),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def audit(
        self,
        event_type: str,
        *,
        target_type: str,
        target_id: str,
        detail: dict[str, Any] | str,
        operator: str = "orchestrator",
    ) -> None:
        if not await self.table_exists("audit_logs"):
            return
        payload = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)
        await self._insert(
            "audit_logs",
            {
                "event_type": event_type,
                "target_type": target_type,
                "target_id": target_id,
                "detail": payload,
                "operator": operator,
                "created_at": _detail_created_at(detail),
            },
        )

    async def record_agent_run(
        self,
        *,
        message_id: int,
        session_id: int | None,
        runtime_type: str,
        status: str,
        started_at: str,
        ended_at: str,
        input_path: str,
        output_path: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float = 0.0,
        error_message: str = "",
    ) -> None:
        if not await self.table_exists("agent_runs"):
            return
        await self._insert(
            "agent_runs",
            {
                "message_id": message_id,
                "session_id": session_id,
                "agent_name": "orchestrator",
                "runtime_type": runtime_type,
                "status": status,
                "started_at": started_at,
                "ended_at": ended_at,
                "input_path": input_path,
                "output_path": output_path,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost_usd": cost_usd,
                "error_message": error_message,
            },
        )

    async def _current_agent_runtime(self) -> str:
        override = ""
        if await self.table_exists("app_settings"):
            async with self._connect() as db:
                cursor = await db.execute(
                    "SELECT value FROM app_settings WHERE key = ?",
                    ("current_agent_runtime",),
                )
                row = await cursor.fetchone()
                override = str(row[0] or "").strip() if row else ""
        try:
            return normalize_agent_runtime(
                override or settings.default_agent_runtime or DEFAULT_AGENT_RUNTIME
            )
        except ValueError:
            return DEFAULT_AGENT_RUNTIME

    async def table_exists(self, table: str) -> bool:
        if self._tables_cache is None:
            async with self._connect() as db:
                cursor = await db.execute("SELECT name FROM sqlite_master WHERE type='table'")
                self._tables_cache = {row[0] for row in await cursor.fetchall()}
        return table in self._tables_cache

    async def table_columns(self, table: str) -> set[str]:
        if table not in self._columns_cache:
            async with self._connect() as db:
                cursor = await db.execute(f"PRAGMA table_info({table})")
                self._columns_cache[table] = {row[1] for row in await cursor.fetchall()}
        return self._columns_cache[table]

    @asynccontextmanager
    async def _connect(self):
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            yield db

    async def _insert(self, table: str, values: dict[str, Any]) -> int | None:
        columns = await self.table_columns(table)
        filtered = {key: value for key, value in values.items() if key in columns}
        if not filtered:
            return None
        names = list(filtered)
        placeholders = ", ".join("?" for _ in names)
        column_sql = ", ".join(names)
        async with self._connect() as db:
            cursor = await db.execute(
                f"INSERT INTO {table} ({column_sql}) VALUES ({placeholders})",
                [filtered[name] for name in names],
            )
            await db.commit()
            return int(cursor.lastrowid) if cursor.lastrowid else None

    async def _update_by_id(self, table: str, row_id: int, fields: dict[str, Any]) -> None:
        columns = await self.table_columns(table)
        filtered = {key: value for key, value in fields.items() if key in columns}
        if not filtered:
            return
        set_clause = ", ".join(f"{key} = ?" for key in filtered)
        async with self._connect() as db:
            await db.execute(
                f"UPDATE {table} SET {set_clause} WHERE id = ?",
                [*filtered.values(), row_id],
            )
            await db.commit()


def _detail_created_at(detail: dict[str, Any] | str) -> str:
    if isinstance(detail, dict):
        value = detail.get("created_at")
        if value:
            return str(value)
    from datetime import datetime

    return datetime.now().isoformat()
