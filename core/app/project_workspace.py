"""Session-scoped multi-project workspace registry."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from loguru import logger

from core.app.context import PreparedWorkspace
from core.projects import ProjectRuntimeContext, get_projects, prepare_project_runtime_context


PROJECT_WORKSPACE_SCHEMA_VERSION = "1.0"
PROJECT_WORKSPACE_FILENAME = "project_workspace.json"
AGENT_SESSIONS_KEY = "agent_sessions"


def project_workspace_path(workspace: PreparedWorkspace) -> Path:
    if not workspace.artifact_roots.get("session_dir"):
        return workspace.input_dir / PROJECT_WORKSPACE_FILENAME
    return _project_workspace_path_from_roots(workspace.artifact_roots)


def ensure_project_workspace(workspace: PreparedWorkspace) -> dict[str, Any]:
    artifact_roots = _artifact_roots_or_default(workspace)
    payload = _read_project_workspace(project_workspace_path(workspace))
    if not payload:
        payload = _empty_project_workspace_payload(artifact_roots)
    else:
        payload = _normalize_project_workspace_payload(payload, artifact_roots)
    _persist_project_workspace(workspace, payload)
    return payload


def load_project_workspace(workspace: PreparedWorkspace) -> dict[str, Any]:
    payload = _read_project_workspace(project_workspace_path(workspace))
    if payload:
        return _normalize_project_workspace_payload(payload, _artifact_roots_or_default(workspace))

    context_path = workspace.input_dir / "project_context.json"
    try:
        context = json.loads(context_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    value = context.get("project_workspace") if isinstance(context, dict) else None
    return value if isinstance(value, dict) else {}


def prepare_project_in_workspace(
    workspace: PreparedWorkspace,
    project_name: str,
    *,
    ones_result: dict[str, Any] | None = None,
    reason: str = "",
    active: bool = True,
    worktree_token: str = "",
) -> dict[str, Any] | None:
    worktree_root = Path(
        workspace.artifact_roots.get("worktrees_dir")
        or (Path(workspace.artifact_roots["session_dir"]) / "worktrees")
    )
    runtime_context = prepare_project_runtime_context(
        project_name,
        ones_result=ones_result,
        worktree_root=worktree_root,
        worktree_token=worktree_token,
    )
    if not runtime_context:
        return None
    return persist_project_runtime(
        workspace,
        runtime_context,
        reason=reason,
        active=active,
    )


def prepare_project_from_session_workspace_path(
    project_name: str,
    *,
    session_workspace_path: Path,
    ones_result: dict[str, Any] | None = None,
    reason: str = "",
    active: bool = True,
    worktree_token: str = "",
) -> dict[str, Any] | None:
    workspace = _workspace_from_session_workspace_path(session_workspace_path)
    if not workspace:
        return None
    return prepare_project_in_workspace(
        workspace,
        project_name,
        ones_result=ones_result,
        reason=reason,
        active=active,
        worktree_token=worktree_token,
    )


def persist_project_runtime(
    workspace: PreparedWorkspace,
    runtime_context: ProjectRuntimeContext | dict[str, Any],
    *,
    reason: str = "",
    active: bool = True,
) -> dict[str, Any]:
    payload = ensure_project_workspace(workspace)
    entry = _runtime_entry(runtime_context, workspace=workspace, reason=reason)
    project_name = str(entry.get("running_project") or entry.get("name") or "").strip()
    if not project_name:
        raise ValueError("runtime context has no running_project")

    projects = payload.setdefault("projects", {})
    if not isinstance(projects, dict):
        projects = {}
        payload["projects"] = projects
    projects[project_name] = entry

    order = payload.setdefault("project_order", [])
    if not isinstance(order, list):
        order = []
        payload["project_order"] = order
    if project_name not in order:
        order.append(project_name)

    if active or not str(payload.get("active_project") or "").strip():
        payload["active_project"] = project_name

    _persist_project_workspace(workspace, payload)
    return entry


def project_agent_session_entry(
    project_workspace: dict[str, Any] | None,
    project_name: str,
    *,
    runtime: str = "",
    role: str = "project",
) -> dict[str, Any]:
    """Return the saved project agent session when it still matches the worktree."""

    if not project_workspace:
        return {}
    projects = project_workspace.get("projects")
    if not isinstance(projects, dict):
        return {}
    project_entry = projects.get(project_name)
    if not isinstance(project_entry, dict):
        return {}

    agent_sessions = project_workspace.get(AGENT_SESSIONS_KEY)
    if not isinstance(agent_sessions, dict):
        return {}
    project_sessions = agent_sessions.get("projects")
    if not isinstance(project_sessions, dict):
        return {}
    saved = project_sessions.get(project_name)
    if not isinstance(saved, dict):
        return {}
    if role and str(saved.get("role") or "project") != role:
        return {}
    if runtime and str(saved.get("runtime") or "") != runtime:
        return {}
    if not str(saved.get("session_id") or "").strip():
        return {}
    if not _agent_session_matches_project(saved, project_entry):
        return {}
    return dict(saved)


def get_project_agent_session_id(
    project_workspace: dict[str, Any] | None,
    project_name: str,
    *,
    runtime: str = "",
    role: str = "project",
) -> str | None:
    entry = project_agent_session_entry(
        project_workspace,
        project_name,
        runtime=runtime,
        role=role,
    )
    session_id = str(entry.get("session_id") or "").strip()
    return session_id or None


def save_project_agent_session(
    *,
    session_workspace_path: Path,
    project_name: str,
    session_id: str,
    runtime: str,
    role: str = "project",
    skill: str = "",
) -> dict[str, Any]:
    """Persist a project-scoped agent session id in project_workspace.json."""

    workspace = _workspace_from_session_workspace_path(session_workspace_path)
    if not workspace:
        return {}

    payload = ensure_project_workspace(workspace)
    projects = payload.get("projects")
    if not isinstance(projects, dict):
        return {}
    project_entry = projects.get(project_name)
    if not isinstance(project_entry, dict):
        return {}

    session_id = str(session_id or "").strip()
    if not session_id:
        return {}

    agent_sessions = payload.setdefault(AGENT_SESSIONS_KEY, {})
    if not isinstance(agent_sessions, dict):
        agent_sessions = {}
        payload[AGENT_SESSIONS_KEY] = agent_sessions
    project_sessions = agent_sessions.setdefault("projects", {})
    if not isinstance(project_sessions, dict):
        project_sessions = {}
        agent_sessions["projects"] = project_sessions

    saved = {
        "session_id": session_id,
        "runtime": str(runtime or ""),
        "role": role,
        "skill": str(skill or ""),
        "worktree_path": str(project_entry.get("worktree_path") or project_entry.get("execution_path") or ""),
        "execution_path": str(project_entry.get("execution_path") or project_entry.get("worktree_path") or ""),
        "checkout_ref": str(project_entry.get("checkout_ref") or ""),
        "execution_commit_sha": str(project_entry.get("execution_commit_sha") or ""),
        "execution_version": str(project_entry.get("execution_version") or ""),
        "project_path": str(project_entry.get("project_path") or project_entry.get("source_path") or ""),
    }
    project_sessions[project_name] = saved
    _persist_project_workspace(workspace, payload)
    return saved


def active_project_entry(project_workspace: dict[str, Any]) -> dict[str, Any]:
    active = str(project_workspace.get("active_project") or "").strip()
    projects = project_workspace.get("projects") if isinstance(project_workspace, dict) else {}
    if isinstance(projects, dict) and active and isinstance(projects.get(active), dict):
        return projects[active]
    if isinstance(projects, dict):
        for value in projects.values():
            if isinstance(value, dict):
                return value
    return {}


def build_project_workspace_prompt_block(project_workspace: dict[str, Any] | None) -> str:
    if not project_workspace:
        return ""
    return (
        "\n\n[Session 项目工作区]\n"
        + json.dumps(project_workspace, ensure_ascii=False, indent=2)
        + "\n\n处理项目问题时，必须在 session worktrees 中工作："
        "source_path/project_path 只表示源仓库登记位置，不作为分析或修改目录。"
        "project_workspace.agent_sessions.projects 保存项目级 Agent resume id，必须按项目名、runtime 和 worktree/commit 匹配后才能复用；"
        "不要把一个项目的 Agent session_id 用到另一个项目。"
        "如果问题涉及尚未加载的相关项目，先调用 prepare_project_worktree，"
        "把该项目加载到 project_workspace.projects 后再检索。"
        "最终回复必须说明本次实际使用的项目、worktree、分支/Tag/版本和提交。"
    )


def _project_workspace_path_from_roots(artifact_roots: dict[str, str]) -> Path:
    return Path(artifact_roots["session_dir"]) / PROJECT_WORKSPACE_FILENAME


def _read_project_workspace(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _empty_project_workspace_payload(artifact_roots: dict[str, str]) -> dict[str, Any]:
    return {
        "schema_version": PROJECT_WORKSPACE_SCHEMA_VERSION,
        "workspace_scope": "session",
        "session_dir": artifact_roots["session_dir"],
        "workspace_dir": artifact_roots["workspace_dir"],
        "worktrees_dir": artifact_roots["worktrees_dir"],
        "active_project": "",
        "project_order": [],
        "projects": {},
        AGENT_SESSIONS_KEY: {"projects": {}},
        "available_projects": _available_project_entries(),
        "policy": {
            "work_directory": "session_worktree",
            "source_path_usage": "registry_only",
            "load_on_demand_tool": "prepare_project_worktree",
        },
    }


def _normalize_project_workspace_payload(
    payload: dict[str, Any],
    artifact_roots: dict[str, str],
) -> dict[str, Any]:
    normalized = dict(payload)
    normalized["schema_version"] = PROJECT_WORKSPACE_SCHEMA_VERSION
    normalized["workspace_scope"] = "session"
    normalized["session_dir"] = artifact_roots["session_dir"]
    normalized["workspace_dir"] = artifact_roots["workspace_dir"]
    normalized["worktrees_dir"] = artifact_roots["worktrees_dir"]
    if not isinstance(normalized.get("projects"), dict):
        normalized["projects"] = {}
    if not isinstance(normalized.get("project_order"), list):
        normalized["project_order"] = list(normalized["projects"])
    if not isinstance(normalized.get(AGENT_SESSIONS_KEY), dict):
        normalized[AGENT_SESSIONS_KEY] = {"projects": {}}
    elif not isinstance(normalized[AGENT_SESSIONS_KEY].get("projects"), dict):
        normalized[AGENT_SESSIONS_KEY]["projects"] = {}
    normalized["available_projects"] = _available_project_entries()
    normalized.setdefault("policy", {})
    if isinstance(normalized["policy"], dict):
        normalized["policy"].update(
            {
                "work_directory": "session_worktree",
                "source_path_usage": "registry_only",
                "load_on_demand_tool": "prepare_project_worktree",
            }
        )
    return normalized


def _artifact_roots_or_default(workspace: PreparedWorkspace) -> dict[str, str]:
    if workspace.artifact_roots.get("session_dir"):
        return workspace.artifact_roots
    session_dir = workspace.path.parent
    return {
        "session_dir": str(session_dir.resolve()),
        "workspace_dir": str(workspace.path.resolve()),
        "worktrees_dir": str((session_dir / "worktrees").resolve()),
    }


def _available_project_entries() -> list[dict[str, str]]:
    return [
        {
            "name": project.name,
            "source_path": str(project.path),
            "description": project.description,
            "status": "registered",
        }
        for project in get_projects()
    ]


def _runtime_entry(
    runtime_context: ProjectRuntimeContext | dict[str, Any],
    *,
    workspace: PreparedWorkspace,
    reason: str,
) -> dict[str, Any]:
    artifact_roots = _artifact_roots_or_default(workspace)
    payload = (
        runtime_context.to_payload()
        if hasattr(runtime_context, "to_payload")
        else dict(runtime_context)
    )
    project_name = str(payload.get("running_project") or "").strip()
    execution_path = str(payload.get("execution_path") or "").strip()
    project_path = str(payload.get("project_path") or "").strip()
    payload.update(
        {
            "name": project_name,
            "status": payload.get("status") or "ready",
            "workspace_scope": "session",
            "session_dir": artifact_roots["session_dir"],
            "workspace_dir": artifact_roots["workspace_dir"],
            "source_path": project_path,
            "worktree_path": execution_path,
            "loaded_reason": reason,
        }
    )
    return payload


def _agent_session_matches_project(saved: dict[str, Any], project_entry: dict[str, Any]) -> bool:
    comparisons = (
        ("worktree_path", "worktree_path"),
        ("execution_path", "execution_path"),
        ("checkout_ref", "checkout_ref"),
        ("execution_commit_sha", "execution_commit_sha"),
    )
    for saved_key, project_key in comparisons:
        saved_value = str(saved.get(saved_key) or "").strip()
        project_value = str(project_entry.get(project_key) or "").strip()
        if saved_value and project_value and saved_value != project_value:
            return False

    saved_worktree = str(saved.get("worktree_path") or saved.get("execution_path") or "").strip()
    project_worktree = str(project_entry.get("worktree_path") or project_entry.get("execution_path") or "").strip()
    if saved_worktree and project_worktree and saved_worktree != project_worktree:
        return False
    return True


def _persist_project_workspace(workspace: PreparedWorkspace, payload: dict[str, Any]) -> None:
    artifact_roots = _artifact_roots_or_default(workspace)
    session_path = project_workspace_path(workspace)
    session_path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    session_path.write_text(text, encoding="utf-8")
    workspace.input_dir.mkdir(parents=True, exist_ok=True)
    (workspace.input_dir / PROJECT_WORKSPACE_FILENAME).write_text(text, encoding="utf-8")

    context_path = workspace.input_dir / "project_context.json"
    try:
        context = json.loads(context_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        context = {"artifact_roots": artifact_roots}
    if not isinstance(context, dict):
        context = {"artifact_roots": artifact_roots}
    context["artifact_roots"] = artifact_roots
    context["project_workspace_path"] = str(session_path.resolve())
    context["project_workspace"] = payload
    context_path.write_text(
        json.dumps(context, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _workspace_from_session_workspace_path(path: Path) -> PreparedWorkspace | None:
    try:
        session_workspace = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        logger.warning("Unable to read session workspace {}: {}", path, exc)
        return None
    roots = session_workspace.get("session_artifact_roots") if isinstance(session_workspace, dict) else None
    if not isinstance(roots, dict):
        return None
    workspace_dir = Path(str(roots.get("workspace_dir") or ""))
    if not workspace_dir:
        return None
    return PreparedWorkspace(
        path=workspace_dir,
        input_dir=workspace_dir / "input",
        state_dir=workspace_dir / "state",
        output_dir=workspace_dir / "output",
        artifacts_dir=workspace_dir / "artifacts",
        artifact_roots={key: str(value) for key, value in roots.items()},
        media_manifest={},
        skill_registry={},
    )
