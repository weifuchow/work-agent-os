from __future__ import annotations

import json
from pathlib import Path

from core.artifacts.session_init import initialize_session_workspace


def test_initialize_session_workspace_creates_static_session_skeleton(tmp_path):
    workspace = tmp_path / "session-42" / "workspace"

    initialized = initialize_session_workspace(workspace, 42)

    assert initialized.workspace == workspace
    for key in (
        "ones_dir",
        "triage_dir",
        "review_dir",
        "worktrees_dir",
        "uploads_dir",
        "attachments_dir",
        "scratch_dir",
    ):
        path = Path(initialized.artifact_roots[key])
        assert path.is_dir()
        assert path.is_relative_to((tmp_path / "session-42").resolve())

    session_workspace = json.loads(initialized.session_workspace_path.read_text(encoding="utf-8"))
    project_context = json.loads((workspace / "input" / "project_context.json").read_text(encoding="utf-8"))
    project_workspace = json.loads(initialized.project_workspace_path.read_text(encoding="utf-8"))
    input_project_workspace = json.loads((workspace / "input" / "project_workspace.json").read_text(encoding="utf-8"))

    assert session_workspace["session_artifact_roots"] == initialized.artifact_roots
    assert project_context["artifact_roots"] == initialized.artifact_roots
    assert project_context["session_workspace_path"] == str(initialized.session_workspace_path.resolve())
    assert project_context["project_workspace_path"] == str(initialized.project_workspace_path.resolve())
    assert project_workspace["projects"] == {}
    assert project_workspace["worktrees_dir"] == initialized.artifact_roots["worktrees_dir"]
    assert input_project_workspace == project_workspace
