from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from loguru import logger

from apps.api.routers import admin, feishu, health
from core.config import settings
from core.database import init_db
from core.logging import setup_logging

# Ensure models are imported so SQLModel registers them
import models.db  # noqa: F401


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    logger.info("Starting {} ...", settings.app_name)
    await init_db()
    logger.info("Database initialized")
    yield
    logger.info("Shutting down {} ...", settings.app_name)


app = FastAPI(
    title=settings.app_name,
    lifespan=lifespan,
)

# CORS — allow Vite dev server in development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers
app.include_router(health.router)
app.include_router(feishu.router)
app.include_router(admin.router)

# Serve admin UI static files (after build)
admin_dist = Path(__file__).resolve().parent.parent / "admin-ui" / "dist"
if admin_dist.exists():
    app.mount("/admin", StaticFiles(directory=str(admin_dist), html=True), name="admin-ui")
