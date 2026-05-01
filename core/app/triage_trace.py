"""Build observable triage workflow traces from session artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.app.context import PreparedWorkspace


def ensure_triage_analysis_traces(workspace: PreparedWorkspace) -> list[Path]:
    triage_root = Path(workspace.artifact_roots.get("triage_dir") or "")
    if not triage_root.exists():
        return []
    written: list[Path] = []
    for topic_dir in sorted(item for item in triage_root.iterdir() if item.is_dir()):
        process_dir = topic_dir / "02-process"
        if not process_dir.exists():
            continue
        trace_path = process_dir / "analysis_trace.json"
        md_path = process_dir / "analysis_trace.md"
        if trace_path.exists() and md_path.exists():
            continue
        trace = build_observable_trace(topic_dir)
        trace_path.write_text(json.dumps(trace, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        md_path.write_text(render_trace_markdown(trace), encoding="utf-8")
        written.extend([trace_path, md_path])
    return written


def build_observable_trace(topic_dir: Path) -> dict[str, Any]:
    state = _read_json(topic_dir / "00-state.json")
    final_decision = _read_json(topic_dir / "02-process" / "final_decision.json")
    steps: list[dict[str, Any]] = []

    if state:
        steps.append({
            "step": "state_initialized",
            "artifact": str(topic_dir / "00-state.json"),
            "summary": state.get("primary_question") or state.get("current_question") or "",
            "details": {
                "project": state.get("project"),
                "version_info": state.get("version_info"),
                "evidence_anchor": state.get("evidence_anchor"),
                "time_alignment": state.get("time_alignment"),
            },
        })

    for package_path in sorted(topic_dir.glob("keyword_package.round*.json")):
        package = _read_json(package_path)
        round_no = package.get("round_no") or _round_from_name(package_path.name)
        dsl_path = topic_dir / f"query.round{round_no}.dsl.txt"
        steps.append({
            "step": "keyword_package_built",
            "round": round_no,
            "artifact": str(package_path),
            "summary": package.get("focus_question") or "",
            "details": {
                "dsl": str(dsl_path) if dsl_path.exists() else "",
                "anchor_terms": package.get("anchor_terms") or [],
                "gate_terms": package.get("gate_terms") or [],
                "target_files": package.get("target_files") or [],
                "preferred_files": package.get("preferred_files") or [],
            },
        })

    for result_path in sorted((topic_dir / "search-runs").glob("*/search_results.json")):
        search = _read_json(result_path)
        package = search.get("keyword_package") if isinstance(search.get("keyword_package"), dict) else {}
        steps.append({
            "step": "log_search_executed",
            "round": package.get("round_no") or _round_from_search_path(result_path),
            "artifact": str(result_path),
            "summary": _search_summary(search),
            "details": {
                "search_root": search.get("search_root"),
                "dsl_query_file": search.get("dsl_query_file"),
                "accepted_hits_total": search.get("accepted_hits_total"),
                "evidence_hits_count": len(search.get("evidence_hits") or []),
                "order_candidates_count": len(search.get("order_candidates") or []),
            },
        })

    if final_decision:
        steps.append({
            "step": "final_decision_recorded",
            "artifact": str(topic_dir / "02-process" / "final_decision.json"),
            "summary": final_decision.get("conclusion") or final_decision.get("summary") or "",
            "details": {
                "status": final_decision.get("status"),
                "confidence": final_decision.get("confidence"),
                "missing_items": final_decision.get("missing_items") or [],
                "next_steps": final_decision.get("next_steps") or [],
            },
        })

    return {
        "schema_version": "1.0",
        "trace_type": "observable_workflow_trace",
        "note": "Records artifacts and decisions visible in the workspace; it is not hidden model chain-of-thought.",
        "topic_dir": str(topic_dir),
        "steps": steps,
    }


def render_trace_markdown(trace: dict[str, Any]) -> str:
    lines = [
        "# Analysis Trace",
        "",
        str(trace.get("note") or ""),
        "",
        f"- Topic: `{trace.get('topic_dir')}`",
        "",
    ]
    for index, step in enumerate(trace.get("steps") or [], start=1):
        lines.append(f"## {index}. {step.get('step')}")
        if step.get("round"):
            lines.append(f"- Round: {step.get('round')}")
        if step.get("summary"):
            lines.append(f"- Summary: {step.get('summary')}")
        if step.get("artifact"):
            lines.append(f"- Artifact: `{step.get('artifact')}`")
        details = step.get("details") if isinstance(step.get("details"), dict) else {}
        for key, value in details.items():
            if value in (None, "", [], {}):
                continue
            rendered = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
            lines.append(f"- {key}: {rendered}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _round_from_name(name: str) -> int | str:
    marker = ".round"
    if marker not in name:
        return ""
    raw = name.split(marker, 1)[1].split(".", 1)[0]
    try:
        return int(raw)
    except ValueError:
        return raw


def _round_from_search_path(path: Path) -> int | str:
    package = _read_json(path).get("keyword_package")
    if isinstance(package, dict) and package.get("round_no"):
        return package["round_no"]
    return ""


def _search_summary(search: dict[str, Any]) -> str:
    accepted = search.get("accepted_hits_total")
    evidence = len(search.get("evidence_hits") or [])
    orders = len(search.get("order_candidates") or [])
    parts = []
    if accepted is not None:
        parts.append(f"accepted_hits={accepted}")
    parts.append(f"evidence_hits={evidence}")
    parts.append(f"order_candidates={orders}")
    return ", ".join(parts)
