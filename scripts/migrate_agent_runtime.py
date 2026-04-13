"""Add agent_runtime column to sessions table."""

from pathlib import Path
import sqlite3


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "db" / "app.sqlite"


def migrate() -> None:
    if not DB_PATH.exists():
        print(f"Database not found at {DB_PATH}, skipping migration.")
        return

    conn = sqlite3.connect(str(DB_PATH))
    try:
        cursor = conn.execute("PRAGMA table_info(sessions)")
        columns = {row[1] for row in cursor.fetchall()}
        if "agent_runtime" not in columns:
            conn.execute("ALTER TABLE sessions ADD COLUMN agent_runtime TEXT DEFAULT 'claude'")
            conn.commit()
            print("Added sessions.agent_runtime")
        else:
            print("sessions.agent_runtime already exists")
    finally:
        conn.close()


if __name__ == "__main__":
    migrate()
