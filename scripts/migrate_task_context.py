"""Add task_contexts table and task_context_id to sessions.

Run: python -m scripts.migrate_task_context
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DB_PATH = Path("data/db/app.sqlite")


def migrate():
    if not DB_PATH.exists():
        print("Database not found. Run init_db first.")
        return

    conn = sqlite3.connect(str(DB_PATH))

    # Create task_contexts table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS task_contexts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title VARCHAR(256) DEFAULT '',
            description TEXT DEFAULT '',
            status VARCHAR(32) DEFAULT 'active',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    print("Created table: task_contexts")

    # Add task_context_id to sessions
    cursor = conn.execute("PRAGMA table_info(sessions)")
    columns = [row[1] for row in cursor.fetchall()]

    if "task_context_id" not in columns:
        conn.execute("ALTER TABLE sessions ADD COLUMN task_context_id INTEGER REFERENCES task_contexts(id)")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_sessions_task_context_id ON sessions(task_context_id)")
        print("Added column: sessions.task_context_id")
    else:
        print("Column sessions.task_context_id already exists.")

    conn.commit()
    conn.close()
    print("Migration complete.")


if __name__ == "__main__":
    migrate()
