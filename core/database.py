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


async def init_db() -> None:
    """Create all tables and ensure data directories exist."""
    for d in [settings.db_dir, settings.memory_dir, settings.reports_dir,
              settings.audit_dir, settings.sessions_dir]:
        Path(d).mkdir(parents=True, exist_ok=True)

    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    _ensure_session_runtime_column()


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Dependency injection for async DB session."""
    async with async_session_factory() as session:
        yield session
