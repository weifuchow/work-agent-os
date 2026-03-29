from pathlib import Path
from typing import Any

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


def load_models_config() -> dict[str, Any]:
    if not settings.models_file.exists():
        return {"default": None, "fallback": None, "models": [], "providers": {}}

    raw = yaml.safe_load(settings.models_file.read_text(encoding="utf-8")) or {}
    providers = raw.get("providers") if isinstance(raw.get("providers"), dict) else {}
    models = raw.get("models") if isinstance(raw.get("models"), list) else []
    default = raw.get("default")
    fallback = raw.get("fallback")

    normalized_models: list[dict[str, Any]] = []
    for item in models:
        if not isinstance(item, dict):
            continue
        model_id = item.get("id")
        provider = item.get("provider")
        if not model_id or not provider:
            continue
        normalized_models.append({
            "id": model_id,
            "provider": provider,
            "label": item.get("label") or model_id,
            "enabled": item.get("enabled", True),
            "supports_chat": item.get("supports_chat", True),
            "supports_agent": item.get("supports_agent", provider == "anthropic"),
            "is_default": model_id == default,
            "is_fallback": model_id == fallback,
        })

    return {
        "default": default,
        "fallback": fallback,
        "models": normalized_models,
        "providers": providers,
    }
