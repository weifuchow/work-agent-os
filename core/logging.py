import sys
from pathlib import Path

from loguru import logger

from core.config import settings


def setup_logging() -> None:
    """Configure loguru with console + file output."""
    logger.remove()

    # Console
    logger.add(
        sys.stderr,
        level=settings.log_level,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level:<8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan> - <level>{message}</level>",
    )

    # File - daily rotation
    log_dir = Path(settings.audit_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    logger.add(
        str(log_dir / "app_{time:YYYY-MM-DD}.log"),
        level="DEBUG",
        rotation="00:00",
        retention="30 days",
        encoding="utf-8",
    )
