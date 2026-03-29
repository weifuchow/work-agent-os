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
from core.connectors.message_service import save_and_process  # noqa: E402
from core.database import async_session_factory, init_db  # noqa: E402
from core.logging import setup_logging  # noqa: E402
from loguru import logger  # noqa: E402

# Reference to the main event loop, set in main() before WS starts.
_main_loop: asyncio.AbstractEventLoop | None = None


def on_message(event_data: dict) -> None:
    """Sync callback from Feishu WS — thread-safely schedule async save."""
    if _main_loop is None or _main_loop.is_closed():
        logger.error("Main event loop not available, dropping message")
        return
    _main_loop.call_soon_threadsafe(
        _main_loop.create_task, _async_on_message(event_data)
    )


async def _async_on_message(event_data: dict) -> None:
    """Persist message to DB and trigger processing pipeline."""
    try:
        async with async_session_factory() as session:
            await save_and_process(session, event_data)
    except Exception as e:
        logger.exception("Failed to save/process message: {}", e)


def main():
    setup_logging()

    if not settings.feishu_app_id or not settings.feishu_app_secret:
        logger.error("FEISHU_APP_ID and FEISHU_APP_SECRET must be set in .env")
        sys.exit(1)

    # Init DB first
    asyncio.run(init_db())

    # Create and hold the main event loop so WS callbacks can schedule tasks.
    global _main_loop
    _main_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_main_loop)

    logger.info("Starting Feishu WebSocket worker...")
    client = FeishuClient(on_message=on_message)

    # start_ws() blocks on its own thread; run the asyncio loop alongside it.
    import threading
    ws_thread = threading.Thread(target=client.start_ws, daemon=True)
    ws_thread.start()

    try:
        _main_loop.run_forever()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Feishu worker stopped")
    finally:
        _main_loop.close()


if __name__ == "__main__":
    main()
