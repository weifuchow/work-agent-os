from __future__ import annotations

import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import core.projects as projects_mod
from core.app.context import MessageContext, PreparedWorkspace
from core.app.project_context import is_direct_project_entry_message, prepare_direct_project_context
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


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
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
        "core.app.project_workspace.prepare_project_runtime_context",
        lambda project_name, ones_result=None, worktree_root=None, worktree_token="": SimpleNamespace(
            running_project=project_name,
            execution_path=Path(worktree_root) / project_name / "current",
            to_payload=lambda: {
                "running_project": project_name,
                "project_path": str(project_path),
                "execution_path": str(Path(worktree_root) / project_name / "current"),
                "current_branch": "release/3.46.x",
                "current_version": "3.46.16",
            },
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
    assert "project_runtime" not in project_context
    project_workspace = json.loads((Path(workspace.artifact_roots["session_dir"]) / "project_workspace.json").read_text(encoding="utf-8"))
    assert project_workspace["active_project"] == "allspark"
    runtime = project_workspace["projects"]["allspark"]
    assert runtime["source_path"] == str(project_path)
    assert runtime["current_version"] == "3.46.16"
    assert Path(runtime["worktree_path"]).parts[-3:] == ("worktrees", "allspark", "current")
    assert (workspace.input_dir / "project_workspace.json").exists()
    assert not (workspace.input_dir / "project_runtime_context.json").exists()


def test_prepare_direct_project_context_fast_entry_creates_master_worktree(monkeypatch, tmp_path):
    workspace = _workspace(tmp_path / "workspace")
    repo = tmp_path / "repo" / "allspark"
    repo.mkdir(parents=True)
    _git(repo, "init", "--initial-branch=master")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "README.md").write_text("master\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "master")
    master_sha = _git(repo, "rev-parse", "--short=12", "HEAD").stdout.strip()
    _git(repo, "checkout", "-b", "feature/current")
    (repo / "README.md").write_text("feature\n", encoding="utf-8")
    _git(repo, "commit", "-am", "feature")

    project = ProjectConfig(
        name="allspark",
        path=repo,
        description="Riot 调度系统 3.0（allspark）。别名：riot3、RIOT3。",
    )
    monkeypatch.setattr("core.app.project_context.get_projects", lambda: [project])
    monkeypatch.setattr(projects_mod, "get_project", lambda name: project if name == "allspark" else None)
    projects_mod._git_ref_inventory.cache_clear()

    ctx = MessageContext(
        message={"id": 1, "content": "allspark"},
        session={"id": 1},
        history=[],
    )

    payload = prepare_direct_project_context(ctx, workspace)

    assert payload is not None
    worktree = Path(payload["worktree_path"])
    assert worktree.exists()
    assert worktree.is_relative_to(Path(workspace.artifact_roots["worktrees_dir"]))
    assert worktree.parts[-3:] == ("worktrees", "allspark", "entry-1-master")
    assert payload["checkout_ref"] == "master"
    assert payload["target_branch_ref"] == "master"
    assert payload["execution_commit_sha"] == master_sha
    assert _git(repo, "branch", "--show-current").stdout.strip() == "feature/current"
    project_workspace = json.loads((Path(workspace.artifact_roots["session_dir"]) / "project_workspace.json").read_text(encoding="utf-8"))
    assert project_workspace["projects"]["allspark"]["worktree_path"] == str(worktree)


def test_direct_project_entry_creates_new_worktree_per_message(monkeypatch, tmp_path):
    workspace = _workspace(tmp_path / "workspace")
    repo = tmp_path / "repo" / "allspark"
    repo.mkdir(parents=True)
    _git(repo, "init", "--initial-branch=master")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "README.md").write_text("master\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "master")

    project = ProjectConfig(
        name="allspark",
        path=repo,
        description="Riot 调度系统 3.0（allspark）。别名：riot3、RIOT3。",
    )
    monkeypatch.setattr("core.app.project_context.get_projects", lambda: [project])
    monkeypatch.setattr(projects_mod, "get_project", lambda name: project if name == "allspark" else None)
    projects_mod._git_ref_inventory.cache_clear()

    first = prepare_direct_project_context(
        MessageContext(message={"id": 10, "content": "allspark"}, session={"id": 1}, history=[]),
        workspace,
    )
    second = prepare_direct_project_context(
        MessageContext(message={"id": 11, "content": "allspark"}, session={"id": 1}, history=[]),
        workspace,
    )

    assert first is not None
    assert second is not None
    assert Path(first["worktree_path"]).parts[-3:] == ("worktrees", "allspark", "entry-10-master")
    assert Path(second["worktree_path"]).parts[-3:] == ("worktrees", "allspark", "entry-11-master")
    assert first["worktree_path"] != second["worktree_path"]
    project_workspace = json.loads((Path(workspace.artifact_roots["session_dir"]) / "project_workspace.json").read_text(encoding="utf-8"))
    assert project_workspace["active_project"] == "allspark"
    assert project_workspace["projects"]["allspark"]["worktree_path"] == second["worktree_path"]


def test_project_followup_from_session_topic_uses_stable_current_worktree(monkeypatch, tmp_path):
    workspace = _workspace(tmp_path / "workspace")
    repo = tmp_path / "repo" / "allspark"
    repo.mkdir(parents=True)
    _git(repo, "init", "--initial-branch=master")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "README.md").write_text("master\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "master")

    project = ProjectConfig(
        name="allspark",
        path=repo,
        description="Riot 调度系统 3.0（allspark）。别名：riot3、RIOT3。",
    )
    monkeypatch.setattr("core.app.project_context.get_projects", lambda: [project])
    monkeypatch.setattr(projects_mod, "get_project", lambda name: project if name == "allspark" else None)
    projects_mod._git_ref_inventory.cache_clear()

    payload = prepare_direct_project_context(
        MessageContext(
            message={"id": 12, "content": "你是基于什么版本分析"},
            session={"id": 1, "title": "allspark", "topic": "allspark 电梯流程"},
            history=[],
        ),
        workspace,
    )

    assert payload is not None
    assert Path(payload["worktree_path"]).parts[-3:] == ("worktrees", "allspark", "current-master")


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
        "core.app.project_workspace.prepare_project_runtime_context",
        lambda project_name, ones_result=None, worktree_root=None, worktree_token="": SimpleNamespace(
            to_payload=lambda: {
                "running_project": project_name,
                "project_path": str(project_path),
                "execution_path": str(Path(worktree_root) / project_name / "current"),
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


def test_prepare_direct_project_context_current_message_overrides_session_project(monkeypatch, tmp_path):
    workspace = _workspace(tmp_path / "workspace")
    allspark_path = tmp_path / "repo" / "allspark"
    sros_path = tmp_path / "repo" / "sros-core"
    allspark_path.mkdir(parents=True)
    sros_path.mkdir(parents=True)

    monkeypatch.setattr(
        "core.app.project_context.get_projects",
        lambda: [
            ProjectConfig(
                name="allspark",
                path=allspark_path,
                description="Riot 调度系统 3.0（allspark）。别名：riot3、RIOT3。",
            ),
            ProjectConfig(
                name="sros-core",
                path=sros_path,
                description="单机本体系统（sros-core）。别名：单机系统、SROS Core、sros。",
            ),
        ],
    )
    monkeypatch.setattr(
        "core.app.project_workspace.prepare_project_runtime_context",
        lambda project_name, ones_result=None, worktree_root=None, worktree_token="": SimpleNamespace(
            to_payload=lambda: {
                "running_project": project_name,
                "project_path": str(allspark_path if project_name == "allspark" else sros_path),
                "execution_path": str(Path(worktree_root) / project_name / "current"),
            }
        ),
    )
    ctx = MessageContext(
        message={"id": 2, "content": "allspark"},
        session={"id": 1, "project": "sros-core"},
        history=[
            {"sender_id": "user", "content": "sros"},
            {"sender_id": "bot", "content": "已进入 sros-core"},
        ],
    )

    payload = prepare_direct_project_context(ctx, workspace)

    assert payload is not None
    assert payload["running_project"] == "allspark"


def test_prepare_direct_project_context_prefers_more_specific_frontend_alias(monkeypatch, tmp_path):
    workspace = _workspace(tmp_path / "workspace")
    allspark_path = tmp_path / "repo" / "allspark"
    frontend_path = tmp_path / "repo" / "riot-frontend-v3"
    allspark_path.mkdir(parents=True)
    frontend_path.mkdir(parents=True)

    monkeypatch.setattr(
        "core.app.project_context.get_projects",
        lambda: [
            ProjectConfig(
                name="allspark",
                path=allspark_path,
                description="Riot 调度系统 3.0（allspark）。别名：riot3、RIOT3、riot3调度系统。",
            ),
            ProjectConfig(
                name="riot-frontend-v3",
                path=frontend_path,
                description="RIOT3 前端项目（riot-frontend-v3）。别名：riot3前端、riot3 前端、RIOT3前端、RIOT3 前端、riot3 frontend、riot3-frontend、allspark frontend。",
            ),
        ],
    )
    monkeypatch.setattr(
        "core.app.project_workspace.prepare_project_runtime_context",
        lambda project_name, ones_result=None, worktree_root=None, worktree_token="": SimpleNamespace(
            to_payload=lambda: {
                "running_project": project_name,
                "project_path": str(frontend_path if project_name == "riot-frontend-v3" else allspark_path),
                "execution_path": str(Path(worktree_root) / project_name / (worktree_token or "current")),
            }
        ),
    )
    ctx = MessageContext(
        message={"id": 992, "content": "RIOT3 前端"},
        session={"id": 164, "project": "allspark"},
        history=[],
    )

    payload = prepare_direct_project_context(ctx, workspace)

    assert payload is not None
    assert payload["running_project"] == "riot-frontend-v3"


def test_prepare_direct_project_context_does_not_reload_current_project_for_followup(monkeypatch, tmp_path):
    workspace = _workspace(tmp_path / "workspace")
    allspark_path = tmp_path / "repo" / "allspark"
    allspark_path.mkdir(parents=True)

    monkeypatch.setattr(
        "core.app.project_context.get_projects",
        lambda: [
            ProjectConfig(
                name="allspark",
                path=allspark_path,
                description="Riot 调度系统 3.0（allspark）。别名：riot3、RIOT3。",
            )
        ],
    )
    ctx = MessageContext(
        message={"id": 979, "content": "车辆推送的数据结构是怎么样的"},
        session={"id": 164, "project": "allspark"},
        history=[],
    )

    assert prepare_direct_project_context(ctx, workspace) is None


def test_direct_project_entry_message_allows_project_switch_after_first_turn(monkeypatch, tmp_path):
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
    ctx = MessageContext(
        message={"id": 2, "content": "allspark"},
        session={"id": 1, "project": "sros-core"},
        history=[
            {"sender_id": "user", "content": "sros"},
            {"sender_id": "bot", "content": "已进入 sros-core"},
            {"sender_id": "user", "content": "allspark"},
        ],
    )

    assert is_direct_project_entry_message(ctx, {"running_project": "allspark"}) is True


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
        "core.app.project_workspace.prepare_project_runtime_context",
        lambda project_name, ones_result=None, worktree_root=None, worktree_token="": SimpleNamespace(
            to_payload=lambda: {
                "running_project": project_name,
                "project_path": str(project_path),
                "execution_path": str(Path(worktree_root) / project_name / "current"),
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
        "core.app.project_workspace.prepare_project_runtime_context",
        lambda project_name, ones_result=None, worktree_root=None, worktree_token="": SimpleNamespace(
            to_payload=lambda: {
                "running_project": project_name,
                "project_path": str(project_path),
                "execution_path": str(Path(worktree_root) / project_name / "current"),
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
