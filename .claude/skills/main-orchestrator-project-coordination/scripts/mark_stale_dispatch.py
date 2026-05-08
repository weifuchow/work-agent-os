from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_failure_artifacts(dispatch_file: Path, error: str) -> None:
    payload = _read_json(dispatch_file)
    if not payload:
        return
    result_path = Path(str(payload.get("result_path") or ""))
    if not result_path:
        analysis_dir = Path(str(payload.get("analysis_dir") or ""))
        result_path = analysis_dir / "result.json" if analysis_dir else Path()
    if result_path:
        _write_json(
            result_path,
            {
                "project": payload.get("project") or "",
                "dispatch_id": payload.get("dispatch_id") or dispatch_file.stem,
                "failed": True,
                "status": "failed",
                "error": error,
                "project_agent_session_id": "",
                "project_agent_session_record_only": True,
                "structured_evidence_summary": [],
            },
        )
    payload.update({"status": "failed", "error": error, "result_path": str(result_path) if result_path else ""})
    _write_json(dispatch_file, payload)


def _fallback_artifact_payload(
    *,
    repo: Path,
    run: dict[str, Any],
    error: str,
) -> dict[str, str]:
    session_id = run.get("session_id")
    message_id = run.get("message_id")
    dispatch_id = f"dispatch-{int(run['id']):03d}"
    session_dir = repo / "data" / "sessions" / f"session-{session_id}"
    project = str(run.get("agent_name") or "").removeprefix("dispatch:") or "project"
    safe_project = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in project.lower()).strip(".-") or "project"
    message_segment = f"message-{message_id}" if message_id else "message-unknown"
    analysis_dir = session_dir / ".analysis" / message_segment / safe_project / dispatch_id
    orchestration_dir = session_dir / ".orchestration" / message_segment
    triage_dir = session_dir / ".triage" / f"{message_segment}-{safe_project}-{dispatch_id}"
    input_path = analysis_dir / "input.json"
    result_path = analysis_dir / "result.json"
    trace_path = analysis_dir / "analysis_trace.md"
    dispatch_file = orchestration_dir / f"{dispatch_id}.json"

    _write_json(
        input_path,
        {
            "message_id": message_id,
            "db_session_id": session_id,
            "dispatch_id": dispatch_id,
            "project_name": project,
            "status": "stale_recovered",
            "error": error,
            "triage_dir": str(triage_dir),
        },
    )
    _write_json(
        result_path,
        {
            "project": project,
            "dispatch_id": dispatch_id,
            "failed": True,
            "status": "failed",
            "error": error,
            "project_agent_session_id": "",
            "project_agent_session_record_only": True,
            "triage_dir": str(triage_dir),
            "structured_evidence_summary": [],
        },
    )
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    trace_path.write_text(
        f"# Project Analysis Trace\n\n- status: failed\n- project: {project}\n- dispatch_id: {dispatch_id}\n- error: {error}\n",
        encoding="utf-8",
    )
    _write_json(
        dispatch_file,
        {
            "dispatch_id": dispatch_id,
            "message_id": message_id,
            "db_session_id": session_id,
            "project": project,
            "analysis_dir": str(analysis_dir),
            "input_path": str(input_path),
            "result_path": str(result_path),
            "triage_dir": str(triage_dir),
            "status": "failed",
            "error": error,
        },
    )
    _write_json(
        triage_dir / "00-state.json",
        {
            "project": project,
            "mode": "structured",
            "phase": "stale_recovered",
            "message_id": message_id,
            "dispatch_id": dispatch_id,
            "artifact_status": "failed",
            "analysis_dir": str(analysis_dir),
            "error": error,
            "agent_context": {
                "runtime": "project-dispatch",
                "skill": "riot-log-triage",
            },
        },
    )
    return {
        "input_path": str(input_path),
        "output_path": str(result_path),
        "dispatch_file": str(dispatch_file),
    }


def mark(repo: Path, older_than_minutes: int, apply: bool) -> dict[str, Any]:
    db_path = repo / "data" / "db" / "app.sqlite"
    cutoff = datetime.now() - timedelta(minutes=older_than_minutes)
    stale: list[dict[str, Any]] = []
    if not db_path.exists():
        return {"updated": 0, "stale": stale, "error": f"db not found: {db_path}"}

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = [
            dict(row)
            for row in conn.execute(
                "SELECT id, session_id, message_id, agent_name, started_at FROM agent_runs "
                "WHERE status = 'running' AND agent_name LIKE 'dispatch:%'"
            )
        ]
        for row in rows:
            started = str(row.get("started_at") or "")
            try:
                started_at = datetime.fromisoformat(started)
            except ValueError:
                started_at = datetime.min
            if started_at >= cutoff:
                continue
            error = f"stale dispatch run after {older_than_minutes} minutes"
            stale.append({**row, "error": error})
            if not apply:
                continue
            paths = {"input_path": "", "output_path": "", "dispatch_file": ""}
            conn.execute(
                "UPDATE agent_runs SET status = 'failed', ended_at = ?, error_message = ? WHERE id = ?",
                (datetime.now().isoformat(), error, row["id"]),
            )
            session_id = row.get("session_id")
            message_id = row.get("message_id")
            if session_id and message_id:
                session_dir = repo / "data" / "sessions" / f"session-{session_id}"
                dispatch_file = (
                    session_dir
                    / ".orchestration"
                    / f"message-{message_id}"
                    / f"dispatch-{int(row['id']):03d}.json"
                )
                if dispatch_file.exists():
                    _write_failure_artifacts(dispatch_file, error)
                    payload = _read_json(dispatch_file)
                    paths = {
                        "input_path": str(payload.get("input_path") or ""),
                        "output_path": str(payload.get("result_path") or ""),
                        "dispatch_file": str(dispatch_file),
                    }
                else:
                    paths = _fallback_artifact_payload(repo=repo, run=row, error=error)
                conn.execute(
                    "UPDATE agent_runs SET input_path = ?, output_path = ? WHERE id = ?",
                    (paths["input_path"], paths["output_path"], row["id"]),
                )
                stale[-1]["dispatch_file"] = paths["dispatch_file"]
        if apply:
            conn.commit()

    return {"updated": len(stale) if apply else 0, "stale": stale, "apply": apply}


def main() -> None:
    parser = argparse.ArgumentParser(description="Mark stale project dispatch agent_runs as failed.")
    parser.add_argument("--repo", default=".", help="work-agent-os repository root")
    parser.add_argument("--older-than-minutes", type=int, default=30)
    parser.add_argument("--apply", action="store_true", help="Actually update DB and artifacts")
    args = parser.parse_args()

    payload = mark(Path(args.repo).resolve(), args.older_than_minutes, args.apply)
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
