"""Migration: add app_settings table for shared runtime settings."""

import sqlite3
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "db" / "app.sqlite"


def migrate():
    if not DB_PATH.exists():
        print(f"Database not found at {DB_PATH}, skipping migration.")
        return

    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        )
        """
    )

    conn.commit()
    conn.close()
    print("Ensured 'app_settings' table exists.")


if __name__ == "__main__":
    migrate()
