from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect read-only work-agent-os session facts.")
    parser.add_argument("--repo", default=".", help="work-agent-os repository root")
    parser.add_argument("--session", required=True, help="Session id, e.g. 173 or session-173")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON")
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    session_id = _parse_session_id(args.session)
    session_dir = repo / "data" / "sessions" / f"session-{session_id}"
    db_path = repo / "data" / "db" / "app.sqlite"

    payload: dict[str, Any] = {
        "repo": str(repo),
        "session_id": session_id,
        "session_dir": str(session_dir),
        "exists": session_dir.exists(),
        "db_path": str(db_path),
        "db_exists": db_path.exists(),
        "files": {},
        "db": {},
        "worktrees": [],
    }

    for name, path in {
        "session_workspace": session_dir / "session_workspace.json",
        "project_workspace": session_dir / "project_workspace.json",
        "input_session": session_dir / "workspace" / "input" / "session.json",
        "input_history": session_dir / "workspace" / "input" / "history.json",
        "input_project_workspace": session_dir / "workspace" / "input" / "project_workspace.json",
    }.items():
        payload["files"][name] = _read_json_file(path)

    worktrees_dir = session_dir / "worktrees"
    if worktrees_dir.exists():
        for item in sorted(worktrees_dir.glob("*/*")):
            if item.is_dir():
                payload["worktrees"].append({
                    "path": str(item),
                    "project": item.parent.name,
                    "name": item.name,
                    "git": _git_summary(item),
                })

    if db_path.exists():
        payload["db"] = _db_facts(db_path, session_id)

    print(json.dumps(payload, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0


def _parse_session_id(value: str) -> int:
    text = str(value).strip().replace("\\", "/").rstrip("/")
    name = text.split("/")[-1]
    if name.startswith("session-"):
        name = name[len("session-"):]
    return int(name)


def _read_json_file(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "path": str(path),
        "exists": path.exists(),
    }
    if not path.exists():
        return result
    try:
        result["data"] = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - diagnostic script
        result["error"] = str(exc)
    return result


def _db_facts(db_path: Path, session_id: int) -> dict[str, Any]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return {
            "session": _fetch_one(conn, "select * from sessions where id = ?", (session_id,)),
            "messages": _fetch_all(
                conn,
                "select id, content, pipeline_status, pipeline_error, processed_at, created_at "
                "from messages where session_id = ? order by id",
                (session_id,),
            ),
            "agent_runs": _fetch_all(
                conn,
                "select id, agent_name, runtime_type, session_id, message_id, status, error_message, started_at, ended_at "
                "from agent_runs where session_id = ? order by id",
                (session_id,),
            ),
            "audit_logs": _audit_logs(conn, session_id),
        }
    finally:
        conn.close()


def _fetch_one(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row else None


def _fetch_all(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def _audit_logs(conn: sqlite3.Connection, session_id: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    message_ids = [
        row["id"]
        for row in conn.execute("select id from messages where session_id = ?", (session_id,)).fetchall()
    ]
    if not message_ids:
        return rows
    placeholders = ",".join("?" for _ in message_ids)
    sql = (
        "select id, event_type, target_type, target_id, detail, created_at from audit_logs "
        f"where target_id in ({placeholders}) order by id"
    )
    for row in conn.execute(sql, tuple(str(mid) for mid in message_ids)).fetchall():
        item = dict(row)
        detail = item.get("detail") or ""
        try:
            item["detail"] = json.loads(detail)
        except Exception:
            item["detail"] = str(detail)[:2000]
        rows.append(item)
    return rows


def _git_summary(path: Path) -> dict[str, str]:
    import subprocess

    def run(*args: str) -> str:
        try:
            completed = subprocess.run(
                ["git", "-C", str(path), *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                check=False,
            )
        except Exception:
            return ""
        return (completed.stdout or "").strip()

    return {
        "branch": run("branch", "--show-current"),
        "commit": run("rev-parse", "--short=12", "HEAD"),
        "describe": run("describe", "--tags", "--always", "--dirty"),
    }


if __name__ == "__main__":
    raise SystemExit(main())
