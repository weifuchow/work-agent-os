"""Initialize database and data directories."""

import asyncio
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import models.db  # noqa: E402, F401 - register models with SQLModel
from core.database import init_db  # noqa: E402


async def main():
    print("Initializing database...")
    await init_db()
    print("Done. Database and directories created.")


if __name__ == "__main__":
    asyncio.run(main())
