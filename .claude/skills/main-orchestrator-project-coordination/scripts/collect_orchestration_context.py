from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _db_path(repo: Path) -> Path:
    return repo / "data" / "db" / "app.sqlite"


def _session_dir(repo: Path, session_id: int) -> Path:
    return repo / "data" / "sessions" / f"session-{session_id}"


def _query_rows(db_path: Path, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


def collect(repo: Path, session_id: int, message_id: int | None) -> dict[str, Any]:
    session_dir = _session_dir(repo, session_id)
    db_path = _db_path(repo)
    target = message_id or 0
    messages_sql = (
        "SELECT id, message_type, content, pipeline_status, pipeline_error, created_at, processed_at "
        "FROM messages WHERE id = ? ORDER BY id"
        if target
        else "SELECT id, message_type, content, pipeline_status, pipeline_error, created_at, processed_at "
             "FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT 8"
    )
    messages_param = (target,) if target else (session_id,)
    run_param = (session_id, target) if target else (session_id,)
    run_sql = (
        "SELECT id, agent_name, runtime_type, status, input_path, output_path, error_message, started_at, ended_at "
        "FROM agent_runs WHERE session_id = ? AND (message_id = ? OR message_id IS NULL) ORDER BY id DESC LIMIT 12"
        if target
        else "SELECT id, agent_name, runtime_type, status, input_path, output_path, error_message, started_at, ended_at "
             "FROM agent_runs WHERE session_id = ? ORDER BY id DESC LIMIT 12"
    )

    artifact_roots = _read_json(session_dir / "session_workspace.json").get("session_artifact_roots") or {}
    message_segment = f"message-{target}" if target else ""
    orchestration_dir = Path(artifact_roots.get("orchestration_dir") or session_dir / ".orchestration")
    analysis_dir = Path(artifact_roots.get("analysis_dir") or session_dir / ".analysis")

    dispatch_files: list[str] = []
    if message_segment:
        dispatch_files = [
            str(path)
            for path in sorted((orchestration_dir / message_segment).glob("dispatch-*.json"))
        ]

    return {
        "session_id": session_id,
        "message_id": message_id,
        "session_dir": str(session_dir),
        "messages": _query_rows(db_path, messages_sql, messages_param),
        "agent_runs": _query_rows(db_path, run_sql, run_param),
        "session_workspace": _read_json(session_dir / "session_workspace.json"),
        "project_workspace": _read_json(session_dir / "project_workspace.json"),
        "uploads": [
            {"path": str(path), "size": path.stat().st_size}
            for path in sorted((session_dir / "uploads").glob("*"))
            if path.is_file()
        ],
        "orchestration_dir": str(orchestration_dir / message_segment) if message_segment else str(orchestration_dir),
        "analysis_dir": str(analysis_dir / message_segment) if message_segment else str(analysis_dir),
        "dispatch_files": dispatch_files,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect main orchestrator context for a session/message.")
    parser.add_argument("--repo", default=".", help="work-agent-os repository root")
    parser.add_argument("--session", type=int, required=True, help="DB/session id")
    parser.add_argument("--message", type=int, default=0, help="Optional DB message id")
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    payload = collect(repo, args.session, args.message or None)
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
