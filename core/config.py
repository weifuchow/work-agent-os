from pathlib import Path
from typing import Any

import sqlite3
import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

# Resolve project root once — all data paths are relative to this.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # App
    app_name: str = "work-agent-os"
    debug: bool = False
    log_level: str = "INFO"

    # Database
    database_url: str = "sqlite+aiosqlite:///data/db/app.sqlite"

    # Anthropic
    anthropic_api_key: str = ""
    anthropic_auth_token: str = ""
    anthropic_base_url: str = ""

    # OpenAI
    openai_api_key: str = ""
    openai_base_url: str = ""

    # Feishu
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_verification_token: str = ""
    feishu_encrypt_key: str = ""
    feishu_bot_name: str = "WorkAgent"

    # Feishu daily report push target
    feishu_report_chat_id: str = ""

    @property
    def project_root(self) -> Path:
        return _PROJECT_ROOT

    @property
    def data_dir(self) -> Path:
        return _PROJECT_ROOT / "data"

    @property
    def db_dir(self) -> Path:
        return self.data_dir / "db"

    @property
    def memory_dir(self) -> Path:
        return self.data_dir / "memory"

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"

    @property
    def audit_dir(self) -> Path:
        return self.data_dir / "audit"

    @property
    def sessions_dir(self) -> Path:
        return self.data_dir / "sessions"

    @property
    def projects_file(self) -> Path:
        return self.data_dir / "projects.yaml"

    @property
    def models_file(self) -> Path:
        return self.data_dir / "models.yaml"


settings = Settings()

_APP_DB_PATH = settings.db_dir / "app.sqlite"
_MODEL_OVERRIDE_KEY = "current_model"


def get_model_override() -> str | None:
    if not _APP_DB_PATH.exists():
        return None

    conn = sqlite3.connect(str(_APP_DB_PATH))
    try:
        cursor = conn.execute(
            "SELECT value FROM app_settings WHERE key = ?",
            (_MODEL_OVERRIDE_KEY,),
        )
        row = cursor.fetchone()
        value = row[0].strip() if row and row[0] else ""
        return value or None
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()


def set_model_override(model_id: str | None) -> None:
    if not _APP_DB_PATH.exists():
        return

    value = (model_id or "").strip()
    conn = sqlite3.connect(str(_APP_DB_PATH))
    try:
        if value:
            conn.execute(
                """
                INSERT INTO app_settings (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                (_MODEL_OVERRIDE_KEY, value, _now_iso()),
            )
        else:
            conn.execute(
                "DELETE FROM app_settings WHERE key = ?",
                (_MODEL_OVERRIDE_KEY,),
            )
        conn.commit()
    except sqlite3.OperationalError:
        return
    finally:
        conn.close()


def _now_iso() -> str:
    from datetime import datetime
    return datetime.now().isoformat()


def load_models_config() -> dict[str, Any]:
    if not settings.models_file.exists():
        return {"default": None, "fallback": None, "models": []}

    raw = yaml.safe_load(settings.models_file.read_text(encoding="utf-8")) or {}
    models = raw.get("models") if isinstance(raw.get("models"), list) else []
    default = raw.get("default")
    fallback = raw.get("fallback")

    normalized_models: list[dict[str, Any]] = []
    for item in models:
        if not isinstance(item, dict):
            continue
        model_id = item.get("id")
        if not model_id:
            continue
        normalized_models.append({
            "id": model_id,
            "label": item.get("label") or model_id,
            "enabled": item.get("enabled", True),
            "is_default": model_id == default,
            "is_fallback": model_id == fallback,
        })

    return {
        "default": default,
        "fallback": fallback,
        "models": normalized_models,
    }


def with_model_state(config: dict[str, Any], override: str | None) -> dict[str, Any]:
    current = override or config.get("default")
    return {
        **config,
        "current": current,
        "override": override,
    }
