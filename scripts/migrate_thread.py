"""Migration: add thread_id/root_id/parent_id to messages and thread_id to sessions."""

import sqlite3
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "db" / "app.sqlite"

MIGRATIONS = [
    # (table, column, type)
    ("messages", "thread_id", "TEXT DEFAULT ''"),
    ("messages", "root_id", "TEXT DEFAULT ''"),
    ("messages", "parent_id", "TEXT DEFAULT ''"),
    ("sessions", "thread_id", "TEXT DEFAULT ''"),
]


def migrate():
    if not DB_PATH.exists():
        print(f"Database not found at {DB_PATH}, skipping migration.")
        return

    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()

    for table, column, col_type in MIGRATIONS:
        cursor.execute(f"PRAGMA table_info({table})")
        columns = {row[1] for row in cursor.fetchall()}

        if column not in columns:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
            print(f"Added '{column}' to {table}.")
        else:
            print(f"Column '{column}' already exists in {table}, skipping.")

    # Create index on messages.thread_id for fast lookup
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS ix_messages_thread_id ON messages(thread_id)"
    )
    # Create index on sessions.thread_id for fast lookup
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS ix_sessions_thread_id ON sessions(thread_id)"
    )

    conn.commit()
    conn.close()
    print("Migration complete.")


if __name__ == "__main__":
    migrate()
