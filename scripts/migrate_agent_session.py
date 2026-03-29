"""Migration: add agent_session_id column to sessions table."""

import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "db" / "app.sqlite"


def migrate():
    if not DB_PATH.exists():
        print(f"Database not found at {DB_PATH}, skipping migration.")
        return

    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()

    # Check if column already exists
    cursor.execute("PRAGMA table_info(sessions)")
    columns = {row[1] for row in cursor.fetchall()}

    if "agent_session_id" not in columns:
        cursor.execute("ALTER TABLE sessions ADD COLUMN agent_session_id TEXT DEFAULT NULL")
        conn.commit()
        print("Added 'agent_session_id' column to sessions table.")
    else:
        print("Column 'agent_session_id' already exists, skipping.")

    conn.close()


if __name__ == "__main__":
    migrate()
