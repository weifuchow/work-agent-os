from pathlib import Path
from typing import Any

import sqlite3
import yaml
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from core.orchestrator.agent_runtime import DEFAULT_AGENT_RUNTIME, normalize_agent_runtime

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
    default_agent_runtime: str = "claude"

    # Feishu
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_verification_token: str = ""
    feishu_encrypt_key: str = ""
    feishu_bot_name: str = "WorkAgent"

    # Feishu daily report push target
    feishu_report_chat_id: str = ""

    @field_validator("debug", mode="before")
    @classmethod
    def _normalize_debug(cls, value: Any) -> Any:
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"release", "prod", "production"}:
                return False
            if normalized in {"debug", "dev", "development"}:
                return True
        return value

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
_AGENT_RUNTIME_OVERRIDE_KEY = "current_agent_runtime"


def _model_override_key(runtime: str | None) -> str:
    if not runtime:
        return _MODEL_OVERRIDE_KEY
    normalized = normalize_agent_runtime(runtime)
    return f"{_MODEL_OVERRIDE_KEY}_{normalized}"


def get_model_override(runtime: str | None = None) -> str | None:
    if not _APP_DB_PATH.exists():
        return None

    conn = sqlite3.connect(str(_APP_DB_PATH))
    try:
        keys: list[str] = []
        if runtime:
            keys.append(_model_override_key(runtime))
            if normalize_agent_runtime(runtime) == "claude":
                keys.append(_MODEL_OVERRIDE_KEY)
        else:
            keys.append(_MODEL_OVERRIDE_KEY)

        for key in keys:
            cursor = conn.execute(
                "SELECT value FROM app_settings WHERE key = ?",
                (key,),
            )
            row = cursor.fetchone()
            value = row[0].strip() if row and row[0] else ""
            if value:
                return value
        return None
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()


def set_model_override(model_id: str | None, runtime: str | None = None) -> None:
    if not _APP_DB_PATH.exists():
        return

    value = (model_id or "").strip()
    key = _model_override_key(runtime)
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
                (key, value, _now_iso()),
            )
            if runtime and normalize_agent_runtime(runtime) == "claude":
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
                (key,),
            )
            if runtime and normalize_agent_runtime(runtime) == "claude":
                conn.execute(
                    "DELETE FROM app_settings WHERE key = ?",
                    (_MODEL_OVERRIDE_KEY,),
                )
        conn.commit()
    except sqlite3.OperationalError:
        return
    finally:
        conn.close()


def get_agent_runtime_override() -> str | None:
    if not _APP_DB_PATH.exists():
        return None

    conn = sqlite3.connect(str(_APP_DB_PATH))
    try:
        cursor = conn.execute(
            "SELECT value FROM app_settings WHERE key = ?",
            (_AGENT_RUNTIME_OVERRIDE_KEY,),
        )
        row = cursor.fetchone()
        value = row[0].strip().lower() if row and row[0] else ""
        return value or None
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()


def set_agent_runtime_override(runtime: str | None) -> None:
    if not _APP_DB_PATH.exists():
        return

    value = (runtime or "").strip().lower()
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
                (_AGENT_RUNTIME_OVERRIDE_KEY, value, _now_iso()),
            )
        else:
            conn.execute(
                "DELETE FROM app_settings WHERE key = ?",
                (_AGENT_RUNTIME_OVERRIDE_KEY,),
            )
        conn.commit()
    except sqlite3.OperationalError:
        return
    finally:
        conn.close()


def _now_iso() -> str:
    from datetime import datetime
    return datetime.now().isoformat()


def infer_model_provider(model_id: str | None) -> str:
    value = (model_id or "").strip().lower()
    if value.startswith("claude"):
        return "claude"
    if value.startswith(("gpt", "o1", "o3", "o4", "codex")):
        return "openai"
    return "unknown"


def model_supported_runtimes(model_id: str | None) -> list[str]:
    provider = infer_model_provider(model_id)
    if provider == "claude":
        return ["claude"]
    if provider == "openai":
        return ["codex"]
    return [DEFAULT_AGENT_RUNTIME]


def get_default_model_for_runtime(
    config: dict[str, Any],
    runtime: str | None,
) -> str | None:
    normalized_runtime = normalize_agent_runtime(runtime or DEFAULT_AGENT_RUNTIME)
    models = [
        item for item in config.get("models", [])
        if normalized_runtime in item.get("supported_runtimes", [])
    ]

    preferred = [config.get("default"), config.get("fallback")]
    for model_id in preferred:
        if any(item.get("id") == model_id for item in models):
            return model_id

    for item in models:
        if item.get("enabled", True):
            return item.get("id")

    return models[0]["id"] if models else None


def filter_models_for_runtime(
    config: dict[str, Any],
    runtime: str | None,
) -> dict[str, Any]:
    normalized_runtime = normalize_agent_runtime(runtime or DEFAULT_AGENT_RUNTIME)
    filtered_models = [
        item for item in config.get("models", [])
        if normalized_runtime in item.get("supported_runtimes", [])
    ]
    default_model = get_default_model_for_runtime(config, normalized_runtime)
    fallback_model = config.get("fallback")
    if fallback_model and not any(item.get("id") == fallback_model for item in filtered_models):
        fallback_model = None

    return {
        **config,
        "default": default_model,
        "fallback": fallback_model,
        "models": filtered_models,
        "runtime": normalized_runtime,
    }


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
        provider = item.get("provider") or infer_model_provider(model_id)
        supported_runtimes = item.get("supported_runtimes") or item.get("runtimes") or model_supported_runtimes(model_id)
        normalized_models.append({
            "id": model_id,
            "label": item.get("label") or model_id,
            "provider": provider,
            "enabled": item.get("enabled", True),
            "supports_chat": item.get("supports_chat", True),
            "supports_agent": item.get("supports_agent", True),
            "supported_runtimes": supported_runtimes,
            "is_default": model_id == default,
            "is_fallback": model_id == fallback,
        })

    return {
        "default": default,
        "fallback": fallback,
        "models": normalized_models,
    }


def with_model_state(config: dict[str, Any], override: str | None, runtime: str | None = None) -> dict[str, Any]:
    current = override or config.get("default")
    return {
        **config,
        "current": current,
        "override": override,
        "runtime": normalize_agent_runtime(runtime or config.get("runtime") or DEFAULT_AGENT_RUNTIME),
    }
