from __future__ import annotations

import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CASESET_DIR = REPO_ROOT / "tests" / "fixtures" / "session_behavior_review_cases"


def _load_case(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_session_behavior_review_cases_have_required_schema():
    case_files = sorted(CASESET_DIR.glob("*.json"))
    assert case_files, "Expected at least one session-behavior-review case fixture."

    required_top_level = {
        "case_id",
        "title",
        "source",
        "symptom",
        "facts",
        "expected_review",
        "expected_debt_decision",
        "must_mention",
        "must_not_claim",
    }
    for path in case_files:
        case = _load_case(path)
        assert required_top_level.issubset(case.keys()), f"Missing required keys in {path.name}"
        assert case["source"]["type"] in {"session", "message", "manual"}
        assert case["source"].get("session_id"), f"Missing session_id in {path.name}"
        assert case["symptom"]["user_question"], f"Missing user question in {path.name}"
        assert case["symptom"]["observed"], f"Missing observed symptom in {path.name}"
        assert case["expected_review"]["quality"] in {"good", "acceptable", "poor", "failed"}
        assert case["expected_review"]["must_identify"], f"Missing must_identify in {path.name}"
        assert case["expected_review"]["root_cause"], f"Missing root_cause in {path.name}"
        assert case["expected_review"]["expected_fix"], f"Missing expected_fix in {path.name}"
        assert case["expected_debt_decision"]["risk"] in {"P0", "P1", "P2", "P3"}
        assert case["expected_debt_decision"]["decision"] in {
            "fix_now",
            "split_task",
            "record_debt",
            "defer",
            "no_change",
        }
        assert case["expected_debt_decision"]["types"], f"Missing debt types in {path.name}"
        assert case["expected_debt_decision"]["acceptance"], f"Missing acceptance in {path.name}"
        assert case["must_mention"], f"Missing must_mention terms in {path.name}"
        assert case["must_not_claim"], f"Missing must_not_claim terms in {path.name}"


def test_session_188_case_captures_worktree_ready_misclassification():
    case = _load_case(CASESET_DIR / "session_188_worktree_context_gap.json")

    assert case["source"]["session_id"] == 188
    assert case["source"]["message_id"] == 1184
    assert case["source"]["project"] == "riot-standalone"
    assert case["facts"]["policy"]["work_directory"] == "session_worktree"
    assert case["facts"]["project_workspace"]["worktree_path"].endswith(r"riot-standalone")
    assert case["facts"]["project_workspace"]["checkout_ref"] == ""
    assert "worktree_context_gap" in case["facts"]["audit_events"]
    assert "sprint/2.1-PD240309" in case["facts"]["ones_version_hints"]
    assert case["expected_review"]["quality"] == "poor"
    assert "dispatch ready check was too broad" in case["expected_review"]["must_identify"]
    assert "dispatch reused any existing path as ready without enforcing session_worktree policy" in case[
        "expected_review"
    ]["root_cause"]
    assert case["expected_debt_decision"]["risk"] == "P1"
    assert case["expected_debt_decision"]["decision"] == "fix_now"
    assert "source repo path is not accepted as ready" in case["expected_debt_decision"]["acceptance"]
