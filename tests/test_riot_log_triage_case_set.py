from __future__ import annotations

import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CASESET_DIR = REPO_ROOT / "tests" / "fixtures" / "riot_log_triage_cases"


def _load_case(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_riot_log_triage_cases_have_required_schema():
    case_files = sorted(CASESET_DIR.glob("*.json"))
    assert case_files, "Expected at least one riot-log-triage case fixture."

    required_top_level = {
        "case_id",
        "title",
        "source",
        "input",
        "artifacts",
        "expected_progression",
        "expected_convergence",
        "acceptance",
        "anti_patterns",
    }
    for path in case_files:
        case = _load_case(path)
        assert required_top_level.issubset(case.keys()), f"Missing required keys in {path.name}"
        assert case["source"]["type"] in {"ones", "session", "manual"}
        assert case["input"]["project"]
        assert case["input"]["vehicle_name"]
        assert case["input"]["problem_time_local"]
        assert isinstance(case["artifacts"]["attachments"], list)
        assert case["expected_progression"]["target_round"] >= 1
        assert case["acceptance"], f"Missing acceptance criteria in {path.name}"
        assert case["anti_patterns"], f"Missing anti-patterns in {path.name}"


def test_case_150295_targets_elevator_cross_map_convergence_without_hints():
    case = _load_case(CASESET_DIR / "ones_150295_ag0019_elevator_stuck.json")

    assert case["source"]["task_number"] == 150295
    assert case["input"]["project"] == "allspark"
    assert case["input"]["version"] == "3.46.23"
    assert case["input"]["vehicle_name"] == "AG0019"
    assert case["input"]["problem_timezone"] == "UTC+8"
    assert case["expected_progression"]["issue_type"] == "order_execution"
    assert case["expected_progression"]["target_process_stage"] == "elevator-cross-map-next-move-gate"
    assert case["expected_progression"]["target_round"] == 2

    prompt = case["input"]["prompt_without_hints"]
    for forbidden_hint in ("CrossMapManager", "vehicleProcState", "HttpAct", "358208"):
        assert forbidden_hint not in prompt

    must_establish = case["expected_progression"]["must_establish"]
    assert "问题时间点车辆状态快照" in must_establish
    assert "乘梯/切图完成后的期望下一步动作" in must_establish

    should_seek_from_logs = case["expected_progression"]["should_seek_from_logs"]
    assert "order_id" in should_seek_from_logs
    assert "ChangeMapRequest completion" in should_seek_from_logs
    assert "vehicleProcState" in should_seek_from_logs

    candidate_gate_terms = case["expected_convergence"]["candidate_gate_terms"]
    assert "CrossMapManager" in candidate_gate_terms
    assert "IN_CHANGE_MAP" in candidate_gate_terms

    anti_patterns = case["anti_patterns"]
    assert any("没有规划路径" in item for item in anti_patterns)
    assert any("HttpAct" in item for item in anti_patterns)
