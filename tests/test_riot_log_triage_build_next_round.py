from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
BUILD_NEXT_ROUND = REPO_ROOT / ".claude" / "skills" / "riot-log-triage" / "scripts" / "build_next_round.py"


def _load_build_next_round():
    module_name = "riot_log_triage_build_next_round_test"
    if str(BUILD_NEXT_ROUND.parent) not in sys.path:
        sys.path.insert(0, str(BUILD_NEXT_ROUND.parent))
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, BUILD_NEXT_ROUND)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def test_build_next_round_generates_dsl_and_updates_state(tmp_path):
    module = _load_build_next_round()
    state_path = tmp_path / ".triage" / "case" / "00-state.json"
    rerank_results_path = tmp_path / ".triage" / "case" / "rerank_results.json"
    _write_json(state_path, {
        "project": "allspark",
        "current_question": "当前 focus",
        "evidence_anchor": {
            "issue_type": "order_execution",
            "vehicle_name": "AG0019",
            "order_id": "",
        },
        "order_candidates": [{"order_id": "358208"}],
        "target_log_files": ["bootstrap", "reservation"],
        "time_alignment": {
            "normalized_window": {
                "start": "2026-04-21 10:04:00",
                "end": "2026-04-21 11:04:00",
            }
        },
        "search_artifacts": {},
        "narrowing_round": {
            "current": 1,
            "history": [
                {
                    "round": 1,
                    "time_window": {
                        "start": "2026-04-21 10:04:00",
                        "end": "2026-04-21 11:04:00",
                    },
                    "target_files": ["bootstrap", "reservation"],
                }
            ],
        },
    })
    _write_json(rerank_results_path, {
        "next_focus_question": "请缩小到 10:34 附近继续查",
        "candidate_order_ids": ["358208"],
        "next_keyword_adjustments": {
            "keep_terms": ["AG0019"],
            "drop_terms": ["hang", "charger"],
            "add_terms": ["ChangeMapRequest", "vehicleProcState", "CrossMapManager"],
            "target_files": ["bootstrap.log.2026-04-21-10.*", "*device*.log*"],
        },
    })

    payload = module.build_next_round(
        rerank_results_path=rerank_results_path,
        state_path=state_path,
        output_dir=tmp_path / ".triage" / "case",
        round_no=2,
    )

    package_path = Path(payload["keyword_package"])
    dsl_path = Path(payload["dsl_query_file"])
    package = json.loads(package_path.read_text(encoding="utf-8"))

    assert package["anchor_terms"] == ["AG0019", "358208"]
    assert "ChangeMapRequest" in package["gate_terms"]
    assert "vehicleProcState" in package["gate_terms"]
    assert package["excluded_files"] == ["hang", "charger"]
    assert package["target_files"] == ["bootstrap.log.2026-04-21-10.*", "*device*.log*"]
    assert package["require_anchor"] is True
    assert '"AG0019"' in payload["dsl_query"]
    assert '"358208"' in payload["dsl_query"]
    assert 'NOT "hang"' in payload["dsl_query"]
    assert dsl_path.read_text(encoding="utf-8").strip() == payload["dsl_query"]

    updated_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert updated_state["current_question"] == "请缩小到 10:34 附近继续查"
    assert updated_state["keyword_package_status"] == "revised"
    assert updated_state["search_artifacts"]["keyword_package_round2"] == str(package_path)
    assert updated_state["search_artifacts"]["dsl_round2"] == str(dsl_path)
    assert updated_state["narrowing_round"]["history"][-1]["next_round"]["round"] == 2
