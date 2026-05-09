"""Artifact, path, and session registry helpers for project dispatch."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
from typing import Any

from loguru import logger

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SESSION_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}


def _project_root() -> Path:
    """Return the active project root, honoring legacy monkeypatches on agent_client."""

    try:
        from core.orchestrator import agent_client as agent_client_mod

        return Path(getattr(agent_client_mod, "PROJECT_ROOT", PROJECT_ROOT))
    except Exception:
        return PROJECT_ROOT


def _app_db_path() -> Path:
    replay_db = str(os.environ.get("WORK_AGENT_REPLAY_DB_PATH") or "").strip()
    if replay_db:
        return Path(replay_db)
    return _project_root() / "data" / "db" / "app.sqlite"


def _sessions_root() -> Path:
    replay_sessions = str(os.environ.get("WORK_AGENT_REPLAY_SESSIONS_DIR") or "").strip()
    if replay_sessions:
        return Path(replay_sessions)
    return _project_root() / "data" / "sessions"


def _session_workspace_path(db_session_id: int) -> Path:
    return _sessions_root() / f"session-{int(db_session_id)}" / "session_workspace.json"


def _message_artifact_segment(message_id: Any) -> str:
    try:
        value = int(message_id)
    except (TypeError, ValueError):
        value = 0
    return f"message-{value}" if value > 0 else "message-unknown"


def _safe_artifact_segment(value: Any, *, fallback: str = "item") -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9._-]+", "-", text)
    text = text.strip(".-")
    return text or fallback


def _write_json_artifact(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _read_json_artifact(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _session_upload_image_paths(artifact_roots: dict[str, str], *, max_images: int = 6) -> list[str]:
    uploads_text = str(artifact_roots.get("uploads_dir") or "").strip()
    if not uploads_text:
        return []
    uploads_dir = Path(uploads_text)
    try:
        candidates = [
            path
            for path in uploads_dir.iterdir()
            if path.is_file() and path.suffix.lower() in SESSION_IMAGE_EXTENSIONS
        ]
    except OSError:
        return []

    image_paths: list[str] = []
    for path in sorted(candidates, key=_mtime, reverse=True)[:max_images]:
        try:
            image_paths.append(str(path.resolve()))
        except OSError:
            image_paths.append(str(path))
    return image_paths


def session_upload_file_paths(artifact_roots: dict[str, str], *, max_files: int = 20) -> list[Path]:
    uploads_text = str(artifact_roots.get("uploads_dir") or "").strip()
    if not uploads_text:
        return []
    uploads_dir = Path(uploads_text)
    try:
        candidates = [path for path in uploads_dir.iterdir() if path.is_file()]
    except OSError:
        return []
    return sorted(candidates, key=_mtime, reverse=True)[:max_files]


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _extract_message_id(input_payload: dict[str, Any]) -> int | None:
    for key in ("message_id", "db_message_id"):
        try:
            value = int(input_payload.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    return None


def _dispatch_id(dispatch_run_id: int | None) -> str:
    if dispatch_run_id:
        return f"dispatch-{int(dispatch_run_id):03d}"
    return "dispatch-untracked"


def _project_entry_from_workspace(
    project_workspace: dict[str, Any] | None,
    project_name: str,
) -> dict[str, Any]:
    projects = project_workspace.get("projects") if isinstance(project_workspace, dict) else None
    entry = projects.get(project_name) if isinstance(projects, dict) else None
    return entry if isinstance(entry, dict) else {}


def _project_entry_is_ready(entry: dict[str, Any]) -> bool:
    path_text = str(entry.get("worktree_path") or entry.get("execution_path") or "").strip()
    if not path_text:
        return False
    status = str(entry.get("status") or "").strip().lower()
    if status and status not in {"ready", "loaded"}:
        return False
    try:
        return Path(path_text).exists()
    except OSError:
        return False


def _project_entry_is_session_worktree_ready(
    entry: dict[str, Any],
    project_workspace: dict[str, Any] | None,
    artifact_roots: dict[str, str] | None,
) -> bool:
    if not _project_entry_is_ready(entry):
        return False

    policy = project_workspace.get("policy") if isinstance(project_workspace, dict) else {}
    work_directory = str((policy or {}).get("work_directory") or "").strip()
    if work_directory != "session_worktree":
        return True

    path_text = str(entry.get("worktree_path") or entry.get("execution_path") or "").strip()
    source_text = str(entry.get("source_path") or entry.get("project_path") or "").strip()
    worktrees_text = str((artifact_roots or {}).get("worktrees_dir") or "").strip()
    if not path_text or not source_text or not worktrees_text:
        return False

    try:
        worktree_path = Path(path_text).resolve()
        source_path = Path(source_text).resolve()
        worktrees_dir = Path(worktrees_text).resolve()
    except OSError:
        return False

    if worktree_path == source_path:
        return False
    try:
        return worktree_path.is_relative_to(worktrees_dir)
    except ValueError:
        return False


def _dispatch_triage_dir(
    *,
    analysis_workspace: str,
    artifact_roots: dict[str, str],
    requested_skill: str,
    selected_skill: str,
    message_segment: str,
    project_segment: str,
    dispatch_id: str,
) -> str:
    existing = _triage_dir_for_analysis_workspace(analysis_workspace)
    if existing:
        return existing
    if (selected_skill or requested_skill) != "riot-log-triage":
        return ""
    triage_root = str(artifact_roots.get("triage_dir") or "").strip()
    if not triage_root:
        return ""
    return str(Path(triage_root) / f"{message_segment}-{project_segment}-{dispatch_id}")


def _triage_dir_for_analysis_workspace(value: str) -> str:
    if not value:
        return ""
    path = Path(value)
    try:
        if (path / "00-state.json").exists():
            return str(path)
    except OSError:
        return ""
    return ""


def _ensure_triage_preflight(
    *,
    triage_dir: str,
    project_name: str,
    message_id: int | None,
    dispatch_id: str,
    task: str,
    context: str,
    analysis_dir: Path | None,
    artifact_roots: dict[str, str],
) -> None:
    if not triage_dir:
        return
    base = Path(triage_dir)
    try:
        (base / "01-intake" / "messages").mkdir(parents=True, exist_ok=True)
        (base / "01-intake" / "attachments").mkdir(parents=True, exist_ok=True)
        (base / "02-process").mkdir(parents=True, exist_ok=True)
        (base / "search-runs").mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("Failed to create triage preflight directories: {}", exc)
        return

    state_path = base / "00-state.json"
    if not state_path.exists():
        _write_json_artifact(
            state_path,
            {
                "project": project_name,
                "mode": "structured",
                "phase": "preflight",
                "message_id": message_id,
                "dispatch_id": dispatch_id,
                "primary_question": task,
                "current_question": task,
                "artifact_status": "preflight",
                "analysis_dir": str(analysis_dir or ""),
                "artifact_roots": artifact_roots,
                "agent_context": {
                    "runtime": "project-dispatch",
                    "skill": "riot-log-triage",
                },
                "missing_items": [],
            },
        )

    _write_json_artifact(
        base / "01-intake" / "messages" / "dispatch_input.json",
        {
            "message_id": message_id,
            "dispatch_id": dispatch_id,
            "project": project_name,
            "task": task,
            "context": context,
            "analysis_dir": str(analysis_dir or ""),
        },
    )


def _persist_project_run_record(
    *,
    session_workspace_path: Path | None,
    project_name: str,
    message_id: int | None,
    dispatch_id: str,
    skill: str,
    analysis_dir: Path | None,
    agent_session_id: str,
    status: str,
) -> dict[str, Any]:
    if not session_workspace_path:
        return {}
    try:
        from core.app.project_workspace import append_project_agent_run

        return append_project_agent_run(
            session_workspace_path=session_workspace_path,
            project_name=project_name,
            run={
                "message_id": message_id,
                "dispatch_id": dispatch_id,
                "skill": skill,
                "analysis_dir": str(analysis_dir) if analysis_dir else "",
                "agent_session_id": agent_session_id,
                "status": status,
            },
        )
    except Exception as exc:
        logger.warning("Failed to append project agent run record: {}", exc)
        return {}


def _write_project_dispatch_failure(
    *,
    project_name: str,
    dispatch_id: str,
    error_text: str,
    result_artifact: Path | None,
    trace_artifact: Path | None,
    dispatch_artifact: Path | None,
    triage_dir: str,
    result_status: str = "failed",
) -> None:
    if result_artifact:
        _write_json_artifact(
            result_artifact,
            {
                "project": project_name,
                "dispatch_id": dispatch_id,
                "failed": True,
                "status": result_status,
                "error": error_text,
                "project_agent_session_id": "",
                "project_agent_session_record_only": True,
                "triage_dir": triage_dir,
                "structured_evidence_summary": [],
            },
        )
    if trace_artifact:
        trace_artifact.write_text(
            f"# Project Analysis Trace\n\n- status: {result_status}\n- project: {project_name}\n- dispatch_id: {dispatch_id}\n- error: {error_text}\n",
            encoding="utf-8",
        )
    if dispatch_artifact:
        payload = _read_json_artifact(dispatch_artifact)
        payload.update({
            "status": result_status,
            "error": error_text,
            "result_path": str(result_artifact or ""),
        })
        _write_json_artifact(dispatch_artifact, payload)
