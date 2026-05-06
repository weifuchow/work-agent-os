from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from core.app.context import MessageContext, PreparedWorkspace
from core.app.project_context import prepare_direct_project_context
from core.projects import ProjectConfig


def _workspace(tmp_path: Path) -> PreparedWorkspace:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    state_dir = tmp_path / "state"
    artifacts_dir = tmp_path / "artifacts"
    session_dir = tmp_path.parent
    for path in (input_dir, output_dir, state_dir, artifacts_dir, session_dir / "worktrees"):
        path.mkdir(parents=True, exist_ok=True)
    artifact_roots = {
        "session_dir": str(session_dir.resolve()),
        "workspace_dir": str(tmp_path.resolve()),
        "worktrees_dir": str((session_dir / "worktrees").resolve()),
    }
    (input_dir / "project_context.json").write_text(
        json.dumps({"artifact_roots": artifact_roots}, ensure_ascii=False),
        encoding="utf-8",
    )
    return PreparedWorkspace(
        path=tmp_path,
        input_dir=input_dir,
        state_dir=state_dir,
        output_dir=output_dir,
        artifacts_dir=artifacts_dir,
        artifact_roots=artifact_roots,
        media_manifest={},
        skill_registry={},
    )


def test_prepare_direct_project_context_persists_runtime(monkeypatch, tmp_path):
    workspace = _workspace(tmp_path / "workspace")
    project_path = tmp_path / "repo" / "allspark"
    project_path.mkdir(parents=True)

    monkeypatch.setattr(
        "core.app.project_context.get_projects",
        lambda: [
            ProjectConfig(
                name="allspark",
                path=project_path,
                description="Riot 调度系统 3.0（allspark）后端/调度核心。别名：riot3、RIOT3。",
            )
        ],
    )
    monkeypatch.setattr(
        "core.app.project_context.resolve_project_runtime_context",
        lambda project_name, worktree_root=None: SimpleNamespace(
            to_payload=lambda: {
                "running_project": project_name,
                "project_path": str(project_path),
                "execution_path": str(project_path),
                "current_branch": "release/3.46.x",
                "current_version": "3.46.16",
                "recommended_worktree": str(Path(worktree_root) / project_name / "current"),
            }
        ),
    )
    ctx = MessageContext(
        message={"id": 1, "content": "allspark"},
        session={"id": 1},
        history=[],
    )

    payload = prepare_direct_project_context(ctx, workspace)

    assert payload is not None
    assert payload["running_project"] == "allspark"
    project_context = json.loads((workspace.input_dir / "project_context.json").read_text(encoding="utf-8"))
    assert project_context["project_runtime"]["project_path"] == str(project_path)
    runtime = json.loads((workspace.input_dir / "project_runtime_context.json").read_text(encoding="utf-8"))
    assert runtime["current_version"] == "3.46.16"
    assert (workspace.output_dir / "project_runtime_context.json").exists()


def test_prepare_direct_project_context_matches_description_alias(monkeypatch, tmp_path):
    workspace = _workspace(tmp_path / "workspace")
    project_path = tmp_path / "repo" / "allspark"
    project_path.mkdir(parents=True)

    monkeypatch.setattr(
        "core.app.project_context.get_projects",
        lambda: [
            ProjectConfig(
                name="allspark",
                path=project_path,
                description="Riot 调度系统 3.0。别名：riot3、RIOT3、riot3调度系统。",
            )
        ],
    )
    monkeypatch.setattr(
        "core.app.project_context.resolve_project_runtime_context",
        lambda project_name, worktree_root=None: SimpleNamespace(
            to_payload=lambda: {
                "running_project": project_name,
                "project_path": str(project_path),
                "execution_path": str(project_path),
            }
        ),
    )
    ctx = MessageContext(
        message={"id": 1, "content": "RIOT3"},
        session={"id": 1},
        history=[],
    )

    payload = prepare_direct_project_context(ctx, workspace)

    assert payload is not None
    assert payload["running_project"] == "allspark"


def test_prepare_direct_project_context_matches_sros_single_system_alias(monkeypatch, tmp_path):
    workspace = _workspace(tmp_path / "workspace")
    project_path = tmp_path / "repo" / "sros-core"
    project_path.mkdir(parents=True)

    monkeypatch.setattr(
        "core.app.project_context.get_projects",
        lambda: [
            ProjectConfig(
                name="sros-core",
                path=project_path,
                description="单机本体系统（sros-core）。别名：单机系统、单机本体系统、SROS Core、sros。",
            )
        ],
    )
    monkeypatch.setattr(
        "core.app.project_context.resolve_project_runtime_context",
        lambda project_name, worktree_root=None: SimpleNamespace(
            to_payload=lambda: {
                "running_project": project_name,
                "project_path": str(project_path),
                "execution_path": str(project_path),
            }
        ),
    )
    ctx = MessageContext(
        message={"id": 1, "content": "单机系统"},
        session={"id": 1},
        history=[],
    )

    payload = prepare_direct_project_context(ctx, workspace)

    assert payload is not None
    assert payload["running_project"] == "sros-core"


def test_prepare_direct_project_context_uses_session_topic(monkeypatch, tmp_path):
    workspace = _workspace(tmp_path / "workspace")
    project_path = tmp_path / "repo" / "allspark"
    project_path.mkdir(parents=True)

    monkeypatch.setattr(
        "core.app.project_context.get_projects",
        lambda: [
            ProjectConfig(
                name="allspark",
                path=project_path,
                description="Riot 调度系统 3.0（allspark）。别名：riot3、RIOT3。",
            )
        ],
    )
    monkeypatch.setattr(
        "core.app.project_context.resolve_project_runtime_context",
        lambda project_name, worktree_root=None: SimpleNamespace(
            to_payload=lambda: {
                "running_project": project_name,
                "project_path": str(project_path),
                "execution_path": str(project_path),
            }
        ),
    )
    ctx = MessageContext(
        message={"id": 2, "content": "你是基于什么版本分析"},
        session={"id": 1, "title": "allspark", "topic": "allspark 电梯流程"},
        history=[],
    )

    payload = prepare_direct_project_context(ctx, workspace)

    assert payload is not None
    assert payload["running_project"] == "allspark"
