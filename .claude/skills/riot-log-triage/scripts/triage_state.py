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
    "primary_question": "",
    "current_question": "",
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
        "problem_timezone": "",
        "log_time_format": "",
        "log_timezone": "",
        "normalized_problem_time": "",
        "normalized_window": {
            "start": "",
            "end": "",
        },
        "status": "unknown",
    },
    "evidence_anchor": {
        "issue_type": "unknown",
        "order_id": "",
        "vehicle_name": "",
        "task_id": "",
        "key_time": "",
        "status": "weak",
    },
    "incident_snapshot": {
        "vehicle_system_state": "",
        "vehicle_proc_state": "",
        "order_stage": "",
        "process_stage": "",
        "current_action": "",
        "expected_next_action": "",
        "status": "unknown",
        "source_evidence": [],
    },
    "module_hypothesis": [],
    "hypotheses": [],
    "noise_candidates": [],
    "target_log_files": [],
    "call_chain_status": "pending",
    "keyword_package_status": "pending",
    "search_status": "pending",
    "evidence_chain_status": "weak",
    "confidence": "low",
    "missing_items": [],
    "order_candidates": [],
    "narrowing_round": {
        "current": 0,
        "history": [],
    },
    "delegation": {
        "search_mode": "",
        "status": "pending",
        "last_scope": "",
        "notes": [],
    },
    "user_confirmation": {
        "formal_report": False,
    },
    "search_artifacts": {
        "last_result_json": "",
        "last_result_md": "",
        "last_rerank_json": "",
        "last_rerank_md": "",
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


def _deep_merge_defaults(defaults: Any, current: Any) -> Any:
    if isinstance(defaults, dict):
        merged: dict[str, Any] = {}
        current_dict = current if isinstance(current, dict) else {}
        for key, value in defaults.items():
            merged[key] = _deep_merge_defaults(value, current_dict.get(key))
        for key, value in current_dict.items():
            if key not in merged:
                merged[key] = value
        return merged
    if current is None:
        return deepcopy(defaults)
    return current


def normalize_state(state: dict[str, Any]) -> dict[str, Any]:
    normalized = _deep_merge_defaults(DEFAULT_STATE_TEMPLATE, state)
    if not normalized.get("created_at"):
        normalized["created_at"] = utc_now_iso()
    if not normalized.get("updated_at"):
        normalized["updated_at"] = normalized["created_at"]
    return normalized


def _anchor_status(*, order_id: str, vehicle_name: str, problem_time: str) -> str:
    if order_id and vehicle_name and problem_time:
        return "strong"
    if order_id or vehicle_name or problem_time:
        return "partial"
    return "weak"


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
    issue_type: str = "",
    order_id: str = "",
    vehicle_name: str = "",
    task_id: str = "",
    primary_question: str = "",
    process_stage: str = "",
) -> dict[str, Any]:
    state = deepcopy(DEFAULT_STATE_TEMPLATE)
    state["project"] = (project or "").strip()
    state["problem_summary"] = (topic or "").strip()
    state["primary_question"] = (primary_question or topic or "").strip()
    state["current_question"] = state["primary_question"]
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
        "problem_timezone": (timezone or "").strip(),
        "log_time_format": "",
        "log_timezone": "",
        "normalized_problem_time": "",
        "normalized_window": {
            "start": "",
            "end": "",
        },
        "status": "unknown",
    }
    resolved_issue_type = (issue_type or "").strip() or (
        "order_execution" if order_id.strip() or vehicle_name.strip() else "unknown"
    )
    state["evidence_anchor"] = {
        "issue_type": resolved_issue_type,
        "order_id": (order_id or "").strip(),
        "vehicle_name": (vehicle_name or "").strip(),
        "task_id": (task_id or "").strip(),
        "key_time": (problem_time or "").strip(),
        "status": _anchor_status(
            order_id=(order_id or "").strip(),
            vehicle_name=(vehicle_name or "").strip(),
            problem_time=(problem_time or "").strip(),
        ),
    }
    state["incident_snapshot"] = {
        "vehicle_system_state": "",
        "vehicle_proc_state": "",
        "order_stage": "",
        "process_stage": (process_stage or "").strip(),
        "current_action": "",
        "expected_next_action": "",
        "status": "unknown",
        "source_evidence": [],
    }
    state["module_hypothesis"] = [module.strip()] if module.strip() else []
    state["missing_items"] = [item.strip() for item in (missing_items or []) if item.strip()]
    if order_id.strip():
        state["order_candidates"] = [
            {
                "order_id": order_id.strip(),
                "hits": 0,
                "sources": ["seed"],
            }
        ]
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
    raw = json.loads(path.read_text(encoding="utf-8"))
    return normalize_state(raw)


def save_state(path: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = utc_now_iso()
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def update_state(path: Path, **updates: Any) -> dict[str, Any]:
    state = load_state(path)
    for key, value in updates.items():
        state[key] = value
    save_state(path, state)
    return state
