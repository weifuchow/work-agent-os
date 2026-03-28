"""Feishu WebSocket long-connection worker.

Run standalone: python -m apps.worker.feishu_worker
"""

import asyncio
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import models.db  # noqa: E402, F401

from core.config import settings  # noqa: E402
from core.connectors.feishu import FeishuClient  # noqa: E402
from core.connectors.message_service import save_message  # noqa: E402
from core.database import async_session_factory, init_db  # noqa: E402
from core.logging import setup_logging  # noqa: E402
from loguru import logger  # noqa: E402


def on_message(event_data: dict) -> None:
    """Sync callback from Feishu WS — schedule async save."""
    asyncio.get_event_loop().create_task(_async_on_message(event_data))


async def _async_on_message(event_data: dict) -> None:
    """Persist message to DB."""
    try:
        async with async_session_factory() as session:
            await save_message(session, event_data)
    except Exception as e:
        logger.exception("Failed to save message: {}", e)


def main():
    setup_logging()

    if not settings.feishu_app_id or not settings.feishu_app_secret:
        logger.error("FEISHU_APP_ID and FEISHU_APP_SECRET must be set in .env")
        sys.exit(1)

    # Init DB first
    asyncio.run(init_db())

    logger.info("Starting Feishu WebSocket worker...")
    client = FeishuClient(on_message=on_message)
    client.start_ws()  # Blocks


if __name__ == "__main__":
    main()
