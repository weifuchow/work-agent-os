from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
RERANK_WORKER = REPO_ROOT / ".claude" / "skills" / "riot-log-triage" / "scripts" / "rerank_worker.py"


def _load_rerank_worker():
    module_name = "riot_log_triage_rerank_worker_test"
    if str(RERANK_WORKER.parent) not in sys.path:
        sys.path.insert(0, str(RERANK_WORKER.parent))
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, RERANK_WORKER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def test_rerank_worker_keeps_only_selected_hits_and_updates_state(tmp_path, monkeypatch):
    module = _load_rerank_worker()
    state_path = tmp_path / ".triage" / "case" / "00-state.json"
    search_results_path = tmp_path / ".triage" / "case" / "search_results.json"
    _write_json(state_path, {
        "project": "allspark",
        "current_question": "为什么后续移动没有继续下发",
        "primary_question": "为什么 AG0019 在订单 358208 上没有继续下发下一段移动",
        "problem_summary": "AG0019 卡在电梯",
        "evidence_anchor": {
            "issue_type": "order_execution",
            "vehicle_name": "AG0019",
            "order_id": "",
        },
        "search_artifacts": {
            "last_result_json": "",
            "last_result_md": "",
            "last_rerank_json": "",
            "last_rerank_md": "",
        },
        "narrowing_round": {
            "current": 1,
            "history": [
                {
                    "round": 1,
                    "focus_question": "为什么后续移动没有继续下发",
                }
            ],
        },
    })
    _write_json(search_results_path, {
        "search_root": str(tmp_path),
        "matched_terms": ["AG0019", "hang"],
        "unmatched_terms": ["ChangeMapRequest"],
        "order_candidates": [{"order_id": "358208", "hits": 3, "sources": ["vehicle_pipe"]}],
        "suppressed_hits_total": 10,
        "evidence_hits": [
            {
                "path": "bootstrap.log",
                "line_number": 10,
                "timestamp": "2026-04-21 10:33:51.444",
                "matched_terms": ["AG0019"],
                "matched_line": "AG0019|358208.Move_xxx: send request finished request:ChangeMapRequest",
                "excerpt": "AG0019|358208.Move_xxx: send request finished request:ChangeMapRequest",
                "match_reason": "anchor + gate",
            },
            {
                "path": "bootstrap.log",
                "line_number": 11,
                "timestamp": "2026-04-21 10:33:56.435",
                "matched_terms": ["AG0019"],
                "matched_line": "AG0019: 切换地图成功,车辆执行状态不符合：IDLE pre：358208，now:358208",
                "excerpt": "AG0019: 切换地图成功,车辆执行状态不符合：IDLE pre：358208，now:358208",
                "match_reason": "anchor + gate",
            },
            {
                "path": "bootstrap.log",
                "line_number": 99,
                "timestamp": "2026-04-21 10:04:01.883",
                "matched_terms": ["AG0019"],
                "matched_line": "AG0019 charging collision",
                "excerpt": "AG0019 charging collision",
                "match_reason": "anchor only",
            },
        ],
    })

    async def fake_request(prompt: str) -> dict:
        assert "h1" in prompt and "h2" in prompt and "h3" in prompt
        return {
            "summary": "关键证据是切图完成和 line:135 状态不符合，充电相关命中是噪音。",
            "relevant_hit_ids": ["h1", "h2"],
            "noise_hit_ids": ["h3"],
            "noise_patterns": ["charging collision noise"],
            "suspected_process_stage": "cross-map-next-move-gate",
            "candidate_order_ids": ["358208"],
            "next_focus_question": "vehicleProcState 为什么在切图成功后不是 IN_CHANGE_MAP",
            "next_keyword_adjustments": {
                "keep_terms": ["AG0019", "ChangeMapRequest", "vehicleProcState"],
                "drop_terms": ["hang"],
                "add_terms": ["CrossMapManager", "IN_CHANGE_MAP"],
                "target_files": ["bootstrap", "reservation"],
            },
            "confidence": "medium",
        }

    monkeypatch.setattr(module, "request_rerank_decision", fake_request)

    payload = asyncio.run(module.rerank_search_results(
        search_results_path=search_results_path,
        state_path=state_path,
        output_dir=tmp_path / ".triage" / "case",
        max_kept_hits=5,
    ))

    assert payload["relevant_hits"] == 2
    assert payload["noise_hits"] == 1
    assert payload["candidate_order_ids"] == ["358208"]

    rerank_json = Path(payload["rerank_json"])
    rerank_results = json.loads(rerank_json.read_text(encoding="utf-8"))
    assert rerank_results["relevant_hit_ids"] == ["h1", "h2"]
    assert rerank_results["noise_hit_ids"] == ["h3"]
    assert rerank_results["relevant_hits"][0]["matched_line"].startswith("AG0019|358208")

    updated_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert updated_state["search_artifacts"]["last_rerank_json"] == str(rerank_json)
    assert updated_state["current_question"] == "vehicleProcState 为什么在切图成功后不是 IN_CHANGE_MAP"
    assert updated_state["order_candidates"][0]["order_id"] == "358208"
    assert updated_state["noise_candidates"] == ["charging collision noise"]
    assert updated_state["narrowing_round"]["history"][-1]["rerank"]["relevant_hit_ids"] == ["h1", "h2"]
