from datetime import datetime

from fastapi import APIRouter
from loguru import logger

router = APIRouter(tags=["health"])


@router.get("/health")
async def health_check():
    checks = {"db": "unknown"}

    # Check DB connectivity
    try:
        import aiosqlite
        from core.config import settings
        db_path = settings.db_dir / "app.sqlite"
        async with aiosqlite.connect(str(db_path)) as db:
            cursor = await db.execute("SELECT COUNT(*) FROM messages")
            count = (await cursor.fetchone())[0]
            checks["db"] = "ok"
            checks["message_count"] = count
    except Exception as e:
        checks["db"] = f"error: {e}"
        logger.warning("Health check DB failed: {}", e)

    overall = "ok" if checks["db"] == "ok" else "degraded"

    return {
        "status": overall,
        "service": "work-agent-os",
        "timestamp": datetime.now().isoformat(),
        "checks": checks,
    }
