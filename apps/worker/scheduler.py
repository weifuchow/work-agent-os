"""APScheduler worker — periodic tasks with no manual intervention needed.

Jobs:
- Task monitor: every 2 min — check stuck tasks, push Feishu alerts (NO LLM)
- Session lifecycle: every 1 hour — auto-freeze/archive stale sessions (NO LLM)
- Memory consolidation: every 6 hours — archive session knowledge (1 LLM call)
- Daily report: every day at 8:00 — generate and push report (1 LLM call)
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from loguru import logger

import models.db  # noqa: F401, E402
from core.config import settings  # noqa: E402
from core.database import init_db  # noqa: E402
from core.logging import setup_logging  # noqa: E402


# ── Job functions ──

async def monitor_job():
    """Check running tasks, notify if stuck. NO LLM."""
    from core.monitor import check_running_tasks
    try:
        counts = await check_running_tasks()
        if counts["stuck"]:
            logger.warning("Monitor: {} stuck tasks found", counts["stuck"])
    except Exception as e:
        logger.exception("Monitor job failed: {}", e)


async def lifecycle_job():
    """Auto-freeze/archive stale sessions. NO LLM."""
    from core.sessions.lifecycle import run_lifecycle_check
    try:
        counts = await run_lifecycle_check()
        if counts["to_waiting"] or counts["to_archived"]:
            logger.info("Lifecycle: {} → waiting, {} → archived",
                        counts["to_waiting"], counts["to_archived"])
    except Exception as e:
        logger.exception("Lifecycle job failed: {}", e)


async def consolidation_job():
    """Archive session knowledge to long-term memory. 1 LLM call per batch."""
    from core.memory.consolidator import consolidate_memories
    try:
        result = await consolidate_memories()
        if result.get("consolidated"):
            logger.info("Consolidation: {} items", result["consolidated"])
    except Exception as e:
        logger.exception("Consolidation job failed: {}", e)


async def daily_report_job():
    """Generate and push daily report. 1 LLM call."""
    from core.reports.daily import generate_daily_report
    try:
        push_chat_id = settings.feishu_report_chat_id
        await generate_daily_report(
            push_to_feishu=bool(push_chat_id),
            push_chat_id=push_chat_id,
        )
    except Exception as e:
        logger.exception("Daily report job failed: {}", e)


# ── Main ──

def main():
    setup_logging()
    asyncio.run(init_db())

    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
    except ImportError:
        logger.error("apscheduler not installed. Run: pip install apscheduler")
        sys.exit(1)

    scheduler = AsyncIOScheduler()

    # NO LLM: check stuck tasks every 2 min
    scheduler.add_job(monitor_job, "interval", minutes=2, id="task_monitor")

    # NO LLM: session lifecycle check every hour
    scheduler.add_job(lifecycle_job, "interval", hours=1, id="lifecycle_check")

    # 1 LLM call: memory consolidation every 6 hours
    scheduler.add_job(consolidation_job, "interval", hours=6, id="memory_consolidation")

    # 1 LLM call: daily report at 8:00 AM
    scheduler.add_job(daily_report_job, "cron", hour=8, minute=0, id="daily_report")

    scheduler.start()
    logger.info(
        "Scheduler started — monitor(2m), lifecycle(1h), consolidation(6h), report(8:00)"
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_forever()
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        logger.info("Scheduler stopped")
    finally:
        loop.close()


if __name__ == "__main__":
    main()
