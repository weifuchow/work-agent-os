"""Add pipeline_status fields to messages table.

Run: python -m scripts.migrate_pipeline
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
    cursor = conn.execute("PRAGMA table_info(messages)")
    columns = [row[1] for row in cursor.fetchall()]

    added = []
    if "pipeline_status" not in columns:
        conn.execute("ALTER TABLE messages ADD COLUMN pipeline_status VARCHAR(32) DEFAULT 'pending'")
        added.append("pipeline_status")
    if "pipeline_error" not in columns:
        conn.execute("ALTER TABLE messages ADD COLUMN pipeline_error TEXT DEFAULT ''")
        added.append("pipeline_error")
    if "processed_at" not in columns:
        conn.execute("ALTER TABLE messages ADD COLUMN processed_at DATETIME")
        added.append("processed_at")

    conn.commit()
    conn.close()

    if added:
        print(f"Added columns: {added}")
    else:
        print("All columns already exist. Nothing to do.")


if __name__ == "__main__":
    migrate()
