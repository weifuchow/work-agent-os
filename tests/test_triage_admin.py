from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from apps.api.routers import admin as admin_mod


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def test_triage_run_to_dict_collects_latest_search(monkeypatch, tmp_path):
    triage_root = tmp_path / ".triage"
    run_dir = triage_root / "order-freeze"
    state_path = run_dir / "00-state.json"
    _write_json(state_path, {
        "project": "allspark",
        "problem_summary": "订单卡死排查",
        "phase": "search_delegated",
        "mode": "structured",
        "confidence": "medium",
        "artifact_completeness": {"status": "partial"},
        "search_status": "returned",
        "evidence_chain_status": "partial",
        "updated_at": "2026-04-15T13:40:00+00:00",
        "created_at": "2026-04-15T13:30:00+00:00",
        "missing_items": ["订单号"],
        "module_hypothesis": ["reservation"],
        "target_log_files": ["reservation.log"],
    })

    search_dir = run_dir / "search-runs" / "20260415_134500"
    _write_json(search_dir / "search_results.json", {
        "hits_total": 2,
        "hits_truncated": False,
        "matched_terms": ["order-123", "ReservationConflictException"],
        "unmatched_terms": ["NotifyService"],
        "top_files": [{"path": "reservation.log", "hits": 2}],
        "evidence_hits": [{
            "path": "reservation.log",
            "line_number": 88,
            "timestamp": "2026-04-15 10:23:10.000",
            "matched_terms": ["order-123"],
            "excerpt": "ERROR order-123 detect conflict",
        }],
    })
    (search_dir / "evidence_summary.md").write_text("# summary\n\n关键证据\n", encoding="utf-8")
    _write_json(run_dir / "02-process" / "routing_decision.json", {
        "route_mode": "direct_project",
        "ones_link_detected": True,
        "pre_project": "",
    })
    _write_json(run_dir / "02-process" / "final_decision.json", {
        "action": "drafted",
        "project_name": "allspark",
        "classified_type": "urgent_issue",
    })
    _write_json(run_dir / "02-process" / "analysis_trace.json", {
        "runtime": "codex",
        "rollout_path": "C:/Users/Standard/.codex/sessions/demo.jsonl",
        "steps": [
            {
                "index": 1,
                "timestamp": "2026-04-15T13:35:00+00:00",
                "kind": "commentary",
                "title": "fetch ones",
                "detail": "先抓 ONES 工单和附件。",
            }
        ],
    })
    (run_dir / "02-process" / "analysis_trace.md").write_text(
        "# Analysis Process\n\n1. fetch ones\n", encoding="utf-8"
    )

    monkeypatch.setattr(admin_mod, "_triage_base_dir", lambda: triage_root)

    payload = admin_mod._triage_run_to_dict(run_dir, include_detail=True)

    assert payload["slug"] == "order-freeze"
    assert payload["project"] == "allspark"
    assert payload["latest_search"] is not None
    assert payload["latest_search"]["hits_total"] == 2
    assert payload["latest_search"]["top_files"][0]["path"] == "reservation.log"
    assert payload["search_runs"][0]["summary_content"].startswith("# summary")
    assert payload["has_process_trace"] is True
    assert payload["route_mode"] == "direct_project"
    assert payload["final_action"] == "drafted"
    assert payload["routing_decision"]["payload"]["ones_link_detected"] is True
    assert payload["final_decision"]["payload"]["project_name"] == "allspark"
    assert payload["analysis_trace"]["runtime"] == "codex"
    assert payload["analysis_trace"]["steps"][0]["title"] == "fetch ones"
    assert payload["analysis_trace"]["markdown_content"].startswith("# Analysis Process")


def test_validate_triage_run_path_blocks_traversal(monkeypatch, tmp_path):
    triage_root = tmp_path / ".triage"
    triage_root.mkdir()
    monkeypatch.setattr(admin_mod, "_triage_base_dir", lambda: triage_root)

    try:
        admin_mod._validate_triage_run_path("../outside")
    except Exception as exc:  # noqa: BLE001
        assert getattr(exc, "status_code", None) == 400
    else:
        raise AssertionError("expected traversal validation to fail")


def test_triage_run_to_dict_uses_session_scoped_slug(monkeypatch, tmp_path):
    sessions_dir = tmp_path / "sessions"
    run_dir = sessions_dir / "session-132" / ".triage" / "order-freeze"
    _write_json(run_dir / "00-state.json", {
        "project": "allspark",
        "problem_summary": "订单取消后小车未解绑",
        "phase": "search_delegated",
        "mode": "structured",
        "confidence": "medium",
        "updated_at": "2026-04-30T13:40:00+08:00",
        "created_at": "2026-04-30T13:30:00+08:00",
    })
    monkeypatch.setattr(admin_mod, "settings", SimpleNamespace(sessions_dir=sessions_dir))

    payload = admin_mod._triage_run_to_dict(run_dir, include_detail=False)

    assert payload["slug"] == "sessions/session-132/order-freeze"
    assert admin_mod._validate_triage_run_path(payload["slug"]) == run_dir.resolve()


def test_list_triage_runs_reads_session_scoped_roots(monkeypatch, tmp_path):
    sessions_dir = tmp_path / "sessions"
    run_dir = sessions_dir / "session-132" / ".triage" / "order-freeze"
    _write_json(run_dir / "00-state.json", {
        "project": "allspark",
        "problem_summary": "订单取消后小车未解绑",
        "phase": "search_delegated",
        "mode": "structured",
        "confidence": "medium",
        "updated_at": "2026-04-30T13:40:00+08:00",
        "created_at": "2026-04-30T13:30:00+08:00",
    })
    monkeypatch.setattr(admin_mod, "settings", SimpleNamespace(sessions_dir=sessions_dir))
    monkeypatch.setattr(admin_mod, "_triage_base_dir", lambda: tmp_path / ".triage")

    bases = admin_mod._triage_base_dirs()

    assert bases == [run_dir.parent]
