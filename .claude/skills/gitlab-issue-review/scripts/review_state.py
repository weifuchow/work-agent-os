from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
import json
from pathlib import Path
import re
from typing import Any


DEFAULT_STATE_TEMPLATE: dict[str, Any] = {
    "project": "",
    "mode": "structured",
    "phase": "initialized",
    "gitlab_collection": {
        "source": "",
        "status": "pending",
        "notes": [],
    },
    "issue": {
        "url": "",
        "host": "",
        "project_path": "",
        "iid": "",
        "title": "",
        "state": "",
        "labels": [],
    },
    "ones_links": [],
    "mr_candidates": [],
    "mr_equivalence_groups": [],
    "branch_targets": [],
    "change_intent": {
        "type": "unknown",
        "signals": [],
        "notes": [],
    },
    "call_logic": {
        "status": "pending",
        "entry_points": [],
        "changed_modules": [],
        "notes": [],
    },
    "review_dimensions": {
        "status": "pending",
        "template": "generic",
        "normal_business_flow": "",
        "anomaly_point": "",
        "trigger_conditions": [],
        "fix_summary": "",
        "side_effect_risks": [],
        "test_validation": {
            "status": "unknown",
            "signals": [],
        },
        "output_notes": [],
    },
    "reply_contract": {
        "format": "rich",
        "title_hint": "",
        "summary_rule": "",
        "section_order": [],
        "table_columns": [],
        "fallback_required": True,
        "notes": [],
    },
    "final_review": {
        "status": "pending",
        "line_comments": [],
        "merge_recommendation": "pending",
        "merge_reason": "",
        "published_to_mr": False,
        "published_mr": {
            "project_path": "",
            "mr_iid": "",
        },
        "published_discussion_ids": [],
        "published_at": "",
        "notes": [],
    },
    "review_scope": {
        "distinct_change_groups": 0,
        "representative_mrs": [],
        "skipped_groups": [],
    },
    "worktree_plan": {
        "status": "pending",
        "notes": [],
    },
    "evidence_chain_status": "weak",
    "confidence": "low",
    "missing_items": [],
    "analysis_artifacts": {
        "last_issue_context_json": "",
        "last_summary_md": "",
    },
    "user_confirmation": {
        "formal_report": False,
    },
}

ISSUE_URL_RE = re.compile(
    r"^(?P<host>https?://[^/]+)/(?P<project_path>.+?)/-/issues/(?P<iid>\d+)(?:[/?#].*)?$",
    flags=re.IGNORECASE,
)


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def slugify_topic(topic: str, max_length: int = 80) -> str:
    normalized = re.sub(r"\s+", "-", (topic or "").strip(), flags=re.UNICODE)
    normalized = re.sub(r"[^\w\-]+", "-", normalized, flags=re.UNICODE)
    normalized = re.sub(r"-{2,}", "-", normalized, flags=re.UNICODE).strip("-_")
    if not normalized:
        normalized = "issue-review"
    return normalized[:max_length].rstrip("-_")


def parse_issue_url(issue_url: str) -> dict[str, str]:
    match = ISSUE_URL_RE.match((issue_url or "").strip())
    if not match:
        return {
            "url": (issue_url or "").strip(),
            "host": "",
            "project_path": "",
            "iid": "",
        }
    return {
        "url": (issue_url or "").strip(),
        "host": match.group("host").strip(),
        "project_path": match.group("project_path").strip(),
        "iid": match.group("iid").strip(),
    }


def build_state(
    *,
    project: str,
    issue_url: str,
    topic: str = "",
    missing_items: list[str] | None = None,
) -> dict[str, Any]:
    state = deepcopy(DEFAULT_STATE_TEMPLATE)
    parsed = parse_issue_url(issue_url)
    state["project"] = (project or "").strip()
    state["issue"] = {
        "url": parsed["url"],
        "host": parsed["host"],
        "project_path": parsed["project_path"],
        "iid": parsed["iid"],
        "title": (topic or "").strip(),
        "state": "",
        "labels": [],
    }
    state["missing_items"] = [item.strip() for item in (missing_items or []) if item.strip()]
    now = utc_now_iso()
    state["created_at"] = now
    state["updated_at"] = now
    return state


def ensure_review_dir(base_dir: Path, topic: str, slug: str | None = None) -> Path:
    target_slug = slugify_topic(slug or topic)
    review_dir = base_dir / target_slug
    review_dir.mkdir(parents=True, exist_ok=True)
    (review_dir / "artifacts").mkdir(parents=True, exist_ok=True)
    return review_dir


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
