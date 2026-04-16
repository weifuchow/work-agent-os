from __future__ import annotations

from copy import deepcopy
from datetime import datetime, UTC
import json
from pathlib import Path
import re
from typing import Any


DEFAULT_STATE_TEMPLATE: dict[str, Any] = {
    "project": "",
    "mode": "structured",
    "phase": "initialized",
    "problem_summary": "",
    "version_info": {
        "value": "",
        "status": "missing",
    },
    "artifact_completeness": {
        "status": "unknown",
        "notes": [],
    },
    "time_alignment": {
        "problem_time": "",
        "log_time_format": "",
        "timezone": "",
        "status": "unknown",
    },
    "module_hypothesis": [],
    "target_log_files": [],
    "call_chain_status": "pending",
    "keyword_package_status": "pending",
    "search_status": "pending",
    "evidence_chain_status": "weak",
    "confidence": "low",
    "missing_items": [],
    "user_confirmation": {
        "formal_report": False,
    },
    "search_artifacts": {
        "last_result_json": "",
        "last_result_md": "",
    },
}


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def slugify_topic(topic: str, max_length: int = 80) -> str:
    normalized = re.sub(r"\s+", "-", (topic or "").strip(), flags=re.UNICODE)
    normalized = re.sub(r"[^\w\-]+", "-", normalized, flags=re.UNICODE)
    normalized = re.sub(r"-{2,}", "-", normalized, flags=re.UNICODE).strip("-_")
    if not normalized:
        normalized = "triage"
    return normalized[:max_length].rstrip("-_")


def build_state(
    *,
    project: str,
    topic: str,
    version: str = "",
    problem_time: str = "",
    module: str = "",
    timezone: str = "",
    artifact_status: str = "unknown",
    missing_items: list[str] | None = None,
) -> dict[str, Any]:
    state = deepcopy(DEFAULT_STATE_TEMPLATE)
    state["project"] = (project or "").strip()
    state["problem_summary"] = (topic or "").strip()
    state["version_info"] = {
        "value": (version or "").strip(),
        "status": "known" if version.strip() else "missing",
    }
    state["artifact_completeness"] = {
        "status": (artifact_status or "unknown").strip(),
        "notes": [],
    }
    state["time_alignment"] = {
        "problem_time": (problem_time or "").strip(),
        "log_time_format": "",
        "timezone": (timezone or "").strip(),
        "status": "unknown",
    }
    state["module_hypothesis"] = [module.strip()] if module.strip() else []
    state["missing_items"] = [item.strip() for item in (missing_items or []) if item.strip()]
    now = utc_now_iso()
    state["created_at"] = now
    state["updated_at"] = now
    return state


def ensure_triage_dir(base_dir: Path, topic: str, slug: str | None = None) -> Path:
    target_slug = slugify_topic(slug or topic)
    triage_dir = base_dir / target_slug
    triage_dir.mkdir(parents=True, exist_ok=True)
    (triage_dir / "search-runs").mkdir(parents=True, exist_ok=True)
    return triage_dir


def load_state(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(path: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = utc_now_iso()
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def update_state(path: Path, **updates: Any) -> dict[str, Any]:
    state = load_state(path)
    for key, value in updates.items():
        state[key] = value
    save_state(path, state)
    return state
