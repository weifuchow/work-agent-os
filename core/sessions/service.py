"""Generic session routing and locking."""

from __future__ import annotations

import asyncio
from typing import Any

from core.repositories import Repository


class SessionService:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository
        self._locks: dict[int, asyncio.Lock] = {}
        self._guard = asyncio.Lock()

    async def resolve_for_message(
        self,
        msg: dict[str, Any],
        *,
        now: str,
    ) -> dict[str, Any] | None:
        existing_id = msg.get("session_id")
        if existing_id:
            session = await self.repository.read_session(int(existing_id))
            if session:
                await self.repository.attach_message_to_session(
                    message_id=int(msg["id"]),
                    session_id=int(session["id"]),
                    now=now,
                )
                return session

        thread_id = str(msg.get("thread_id") or "").strip()
        if thread_id:
            session = await self.repository.find_open_session_by_thread(thread_id)
            if session:
                await self.repository.attach_message_to_session(
                    message_id=int(msg["id"]),
                    session_id=int(session["id"]),
                    now=now,
                )
                return await self.repository.read_session(int(session["id"]))

        if not str(msg.get("chat_id") or "").strip():
            return None

        session = await self.repository.create_session_for_message(msg, now)
        if session:
            await self.repository.attach_message_to_session(
                message_id=int(msg["id"]),
                session_id=int(session["id"]),
                now=now,
            )
            return await self.repository.read_session(int(session["id"]))
        return None

    async def lock_for(self, session_id: int | None) -> asyncio.Lock | None:
        if session_id is None:
            return None
        async with self._guard:
            lock = self._locks.get(session_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[session_id] = lock
            return lock
