from datetime import UTC, datetime

from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health")
async def health_check():
    return {
        "status": "ok",
        "service": "work-agent-os",
        "timestamp": datetime.now(UTC).isoformat(),
    }
