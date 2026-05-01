from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_ones_150552_acceptance_artifacts():
    session_id = os.environ.get("ONES_ACCEPTANCE_SESSION_ID", "").strip()
    if not session_id:
        pytest.skip("Set ONES_ACCEPTANCE_SESSION_ID after running the real ONES acceptance flow.")

    session_dir = ROOT / "data" / "sessions" / f"session-{session_id}"
    order_id = "1777271159592"
    vehicle = "\u65b9\u683c-10008"

    ones_dir = session_dir / ".ones" / "150552_NbJXtiyGP7R4vYnF"
    summary_path = ones_dir / "summary_snapshot.json"
    task_path = ones_dir / "task.json"
    triage_dir = _find_triage_dir(session_dir)
    dsl_path = triage_dir / "query.round1.dsl.txt"
    package_path = triage_dir / "keyword_package.round1.json"
    state_path = triage_dir / "00-state.json"
    process_dir = triage_dir / "02-process"
    final_decision_path = process_dir / "final_decision.json"
    trace_json_path = process_dir / "analysis_trace.json"
    trace_md_path = process_dir / "analysis_trace.md"
    runtime_context_path = session_dir / "workspace" / "input" / "project_runtime_context.json"
    search_results = sorted((triage_dir / "search-runs").glob("*/search_results.json"))

    summary = _read_json(summary_path)
    task = _read_json(task_path)
    package = _read_json(package_path)
    state = _read_json(state_path)
    runtime_context = _read_json(runtime_context_path)
    search = _read_json(search_results[-1]) if search_results else {}
    dsl_text = dsl_path.read_text(encoding="utf-8") if dsl_path.exists() else ""

    image_findings = [str(item) for item in summary.get("image_findings") or []]
    downloaded = summary.get("downloaded_files") or []
    image_downloads = [
        item
        for item in downloaded
        if str(item.get("label") or "").startswith("description_image_")
        and Path(str(item.get("path") or "")).exists()
    ]
    summary_blob = "\n".join([
        str(summary.get("summary_text") or ""),
        *[str(item) for item in summary.get("observations") or []],
        *image_findings,
    ])

    assert summary_path.exists()
    assert str(summary_path.resolve()).startswith(str((session_dir / ".ones").resolve()))
    assert task.get("summary_snapshot")
    assert str(task.get("paths", {}).get("summary_snapshot_json") or "").endswith(
        "summary_snapshot.json"
    )
    assert summary.get("source", {}).get("runtime") == "codex"
    assert (
        summary.get("source", {}).get("summary_status")
        or summary.get("source", {}).get("subagent_status")
    ) == "success"
    assert summary.get("source", {}).get("images_consumed") is True
    assert len(image_downloads) >= 5
    assert len(image_findings) >= 5
    assert order_id in summary_blob
    assert vehicle in summary_blob

    assert _has_finding(image_findings, "\u53d6\u6d88\u8ba2\u5355", "\u6210\u529f", order_id)
    assert _has_finding(image_findings, vehicle, order_id)
    assert _has_finding(image_findings, "\u6267\u884c\u4e2d", vehicle, order_id)
    assert _has_finding(image_findings, "\u8ba2\u5355\u5217\u8868", vehicle, order_id)
    assert _has_finding(image_findings, "\u6267\u884c\u65e5\u5fd7", order_id) or _has_finding(
        image_findings, "\u7528\u6237\u53d6\u6d88", order_id
    )

    assert dsl_path.exists()
    assert order_id in dsl_text
    assert vehicle in dsl_text
    assert order_id in (package.get("anchor_terms") or [])
    assert vehicle in (package.get("anchor_terms") or [])
    assert state.get("evidence_anchor", {}).get("order_id") == order_id
    assert state.get("evidence_anchor", {}).get("vehicle_name") == vehicle
    assert process_dir.is_dir()
    assert final_decision_path.exists()
    assert trace_json_path.exists()
    assert trace_md_path.exists()

    trace = _read_json(trace_json_path)
    trace_steps = [str(item.get("step") or "") for item in trace.get("steps") or []]
    trace_md = trace_md_path.read_text(encoding="utf-8")
    assert trace.get("trace_type") == "observable_workflow_trace"
    for expected in (
        "state_initialized",
        "keyword_package_built",
        "log_search_executed",
        "final_decision_recorded",
    ):
        assert expected in trace_steps
        assert expected in trace_md
    assert str(dsl_path) in trace_md

    execution_path = Path(str(runtime_context.get("execution_path") or ""))
    assert runtime_context.get("running_project") == "allspark"
    assert runtime_context.get("normalized_version") == "3.52.0"
    assert runtime_context.get("checkout_ref") in {"3.52.0", "master"}
    assert str(execution_path).startswith(str((session_dir / "worktrees").resolve()))
    assert runtime_context.get("execution_version") or runtime_context.get("execution_describe")

    assert search_results
    assert Path(str(search.get("dsl_query_file") or "")).resolve() == dsl_path.resolve()
    assert order_id in (search.get("keyword_package", {}).get("anchor_terms") or [])
    assert vehicle in (search.get("keyword_package", {}).get("anchor_terms") or [])
    assert int(search.get("accepted_hits_total") or 0) > 0

    evidence_hits = search.get("evidence_hits") or []
    assert any(order_id in str(hit.get("matched_line") or hit.get("excerpt") or "") for hit in evidence_hits)
    vehicle_samples = [
        sample
        for item in search.get("order_candidates") or []
        for sample in item.get("samples") or []
        if vehicle in str(sample.get("line") or "")
    ]
    assert vehicle_samples


def _read_json(path: Path) -> dict:
    assert path.exists(), f"missing {path}"
    return json.loads(path.read_text(encoding="utf-8"))


def _find_triage_dir(session_dir: Path) -> Path:
    root = session_dir / ".triage"
    candidates = [
        item
        for item in sorted(root.glob("*"))
        if item.is_dir()
        and (item / "keyword_package.round1.json").exists()
        and (item / "query.round1.dsl.txt").exists()
        and (item / "00-state.json").exists()
    ]
    assert candidates, f"missing triage acceptance artifacts under {root}"
    return candidates[-1]


def _has_finding(findings: list[str], *terms: str) -> bool:
    return any(all(term in item for term in terms) for item in findings)
