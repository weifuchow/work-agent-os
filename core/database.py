from collections.abc import AsyncGenerator
from pathlib import Path
import sqlite3

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel

from core.config import settings

# Build absolute DB URL from project root to avoid CWD dependency.
_db_path = settings.db_dir / "app.sqlite"
_db_url = f"sqlite+aiosqlite:///{_db_path}"

engine = create_async_engine(
    _db_url,
    echo=settings.debug,
)

async_session_factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def _ensure_session_runtime_column() -> None:
    if not _db_path.exists():
        return

    conn = sqlite3.connect(str(_db_path))
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
        if "agent_runtime" not in columns:
            conn.execute("ALTER TABLE sessions ADD COLUMN agent_runtime TEXT DEFAULT 'claude'")
            conn.commit()
    finally:
        conn.close()


def _ensure_session_analysis_columns() -> None:
    if not _db_path.exists():
        return

    conn = sqlite3.connect(str(_db_path))
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
        migrations = {
            "analysis_mode": "ALTER TABLE sessions ADD COLUMN analysis_mode INTEGER DEFAULT 0",
            "analysis_workspace": "ALTER TABLE sessions ADD COLUMN analysis_workspace TEXT DEFAULT ''",
        }
        applied = False
        for column, ddl in migrations.items():
            if column not in columns:
                conn.execute(ddl)
                applied = True
        if applied:
            conn.commit()
    finally:
        conn.close()


def _ensure_memory_entry_columns() -> None:
    if not _db_path.exists():
        return

    conn = sqlite3.connect(str(_db_path))
    try:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        if "memory_entries" not in tables:
            return

        columns = {row[1] for row in conn.execute("PRAGMA table_info(memory_entries)").fetchall()}
        migrations = {
            "project_version": "ALTER TABLE memory_entries ADD COLUMN project_version TEXT DEFAULT ''",
            "project_branch": "ALTER TABLE memory_entries ADD COLUMN project_branch TEXT DEFAULT ''",
            "project_commit_sha": "ALTER TABLE memory_entries ADD COLUMN project_commit_sha TEXT DEFAULT ''",
            "project_commit_time": "ALTER TABLE memory_entries ADD COLUMN project_commit_time TEXT DEFAULT NULL",
        }
        applied = False
        for column, ddl in migrations.items():
            if column not in columns:
                conn.execute(ddl)
                applied = True
        if applied:
            conn.commit()
    finally:
        conn.close()


def _ensure_message_media_columns() -> None:
    if not _db_path.exists():
        return

    conn = sqlite3.connect(str(_db_path))
    try:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        if "messages" not in tables:
            return

        columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
        migrations = {
            "media_info_json": "ALTER TABLE messages ADD COLUMN media_info_json TEXT DEFAULT ''",
            "attachment_path": "ALTER TABLE messages ADD COLUMN attachment_path TEXT DEFAULT ''",
        }
        applied = False
        for column, ddl in migrations.items():
            if column not in columns:
                conn.execute(ddl)
                applied = True
        if applied:
            conn.commit()
    finally:
        conn.close()


async def init_db() -> None:
    """Create all tables and ensure data directories exist."""
    for d in [settings.db_dir, settings.memory_dir, settings.reports_dir,
              settings.audit_dir, settings.sessions_dir]:
        Path(d).mkdir(parents=True, exist_ok=True)

    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    _ensure_session_runtime_column()
    _ensure_session_analysis_columns()
    _ensure_memory_entry_columns()
    _ensure_message_media_columns()


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Dependency injection for async DB session."""
    async with async_session_factory() as session:
        yield session
