"""Initialize session-scoped workspace directories and static manifests."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.app.context import PreparedWorkspace
from core.app.project_workspace import ensure_project_workspace
from core.artifacts.manifest import write_json


@dataclass(frozen=True)
class InitializedSessionWorkspace:
    workspace: Path
    input_dir: Path
    state_dir: Path
    output_dir: Path
    artifacts_dir: Path
    artifact_roots: dict[str, str]
    session_workspace_path: Path
    project_workspace_path: Path


def initialize_session_workspace(workspace: Path, session_id: int | None) -> InitializedSessionWorkspace:
    artifact_roots = artifact_roots_for(workspace, session_id)
    input_dir = workspace / "input"
    state_dir = workspace / "state"
    output_dir = workspace / "output"
    artifacts_dir = workspace / "artifacts"
    for path in (
        input_dir,
        state_dir,
        output_dir,
        artifacts_dir,
        *[Path(value) for value in artifact_roots.values()],
    ):
        path.mkdir(parents=True, exist_ok=True)

    session_workspace_path = Path(artifact_roots["session_dir"]) / "session_workspace.json"
    project_workspace_path = Path(artifact_roots["session_dir"]) / "project_workspace.json"
    write_json(
        session_workspace_path,
        session_workspace_map(artifact_roots=artifact_roots),
    )
    write_json(
        input_dir / "project_context.json",
        {
            "artifact_roots": artifact_roots,
            "session_workspace_path": str(session_workspace_path.resolve()),
            "project_workspace_path": str(project_workspace_path.resolve()),
        },
    )
    ensure_project_workspace(
        PreparedWorkspace(
            path=workspace,
            input_dir=input_dir,
            state_dir=state_dir,
            output_dir=output_dir,
            artifacts_dir=artifacts_dir,
            artifact_roots=artifact_roots,
            media_manifest={},
            skill_registry={},
        )
    )

    return InitializedSessionWorkspace(
        workspace=workspace,
        input_dir=input_dir,
        state_dir=state_dir,
        output_dir=output_dir,
        artifacts_dir=artifacts_dir,
        artifact_roots=artifact_roots,
        session_workspace_path=session_workspace_path,
        project_workspace_path=project_workspace_path,
    )


def artifact_roots_for(workspace: Path, session_id: int | None) -> dict[str, str]:
    session_dir = workspace.parent
    return {
        "session_dir": str(session_dir.resolve()),
        "workspace_dir": str(workspace.resolve()),
        "ones_dir": str((session_dir / ".ones").resolve()),
        "triage_dir": str((session_dir / ".triage").resolve()),
        "review_dir": str((session_dir / ".review").resolve()),
        "orchestration_dir": str((session_dir / ".orchestration").resolve()),
        "analysis_dir": str((session_dir / ".analysis").resolve()),
        "worktrees_dir": str((session_dir / "worktrees").resolve()),
        "uploads_dir": str((session_dir / "uploads").resolve()),
        "attachments_dir": str((session_dir / "attachments").resolve()),
        "scratch_dir": str((session_dir / "scratch").resolve()),
    }


def session_workspace_map(
    *,
    artifact_roots: dict[str, str],
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "session_dir": artifact_roots["session_dir"],
        "workspace_dir": artifact_roots["workspace_dir"],
        "session_artifact_roots": artifact_roots,
        "orchestration": {
            "dir": ".orchestration",
            "policy": {
                "main_orchestrator_required": True,
                "project_routing_owner": "main_orchestrator",
                "project_agent_resume": False,
                "final_reply_owner": "main_orchestrator",
            },
        },
        "analysis": {
            "dir": ".analysis",
            "project_agent_outputs_required": True,
        },
        "lookup_order": [
            "workspace/input/message.json",
            "workspace/input/session.json",
            "workspace/input/history.json",
            "workspace/input/media_manifest.json",
            "workspace/input/skill_registry.json",
            "workspace/input/project_workspace.json",
            "project_workspace.json",
            "session_artifact_roots.uploads_dir",
            "session_artifact_roots.ones_dir",
            "session_artifact_roots.triage_dir",
            "session_artifact_roots.review_dir",
            "session_artifact_roots.orchestration_dir",
            "session_artifact_roots.analysis_dir",
            "session_artifact_roots.worktrees_dir",
        ],
        "write_policy": {
            "final_reply_contract": "workspace/output",
            "structured_summary": "workspace/output",
            "runtime_state": "workspace/state",
            "raw_user_uploads": "session_artifact_roots.uploads_dir",
            "extracted_or_curated_attachments": "session_artifact_roots.attachments_dir",
            "ones_artifacts": "session_artifact_roots.ones_dir",
            "triage_artifacts": "session_artifact_roots.triage_dir/<topic>",
            "triage_intake": "session_artifact_roots.triage_dir/<topic>/01-intake",
            "triage_process": "session_artifact_roots.triage_dir/<topic>/02-process",
            "triage_search_runs": "session_artifact_roots.triage_dir/<topic>/search-runs",
            "triage_state_and_dsl": "session_artifact_roots.triage_dir/<topic>/00-state.json + keyword_package.roundN.json + query.roundN.dsl.txt",
            "review_artifacts": "session_artifact_roots.review_dir",
            "project_worktrees": "session_artifact_roots.worktrees_dir/<project>/<task-version>",
            "project_workspace_registry": "project_workspace.json + workspace/input/project_workspace.json",
            "orchestration_plan": "session_artifact_roots.orchestration_dir/<message-id>/plan.json",
            "orchestration_review": "session_artifact_roots.orchestration_dir/<message-id>/review.json",
            "project_analysis": "session_artifact_roots.analysis_dir/<message-id>/<project>/<dispatch-id>",
            "scratch_files": "session_artifact_roots.scratch_dir",
        },
        "forbidden_roots": [
            ".ones",
            ".triage",
            ".review",
            ".session",
            "data/attachments",
        ],
    }
