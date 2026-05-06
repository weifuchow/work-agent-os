import core.projects as projects_mod
from core.projects import infer_version_from_git


def test_infer_version_from_git_prefers_semver_from_branch():
    assert infer_version_from_git(
        branch="release/3.0",
        describe="v2.9.1-3-gabc123",
        commit_sha="abc123456789",
    ) == "3.0"


def test_infer_version_from_git_falls_back_to_branch_and_commit():
    assert infer_version_from_git(
        branch="feature/podman-support",
        describe="",
        commit_sha="abc123456789",
    ) == "feature/podman-support@abc12345"


def test_get_projects_reload_when_projects_file_changes(monkeypatch, tmp_path):
    projects_file = tmp_path / "projects.yaml"
    project_one = tmp_path / "one"
    project_two = tmp_path / "two"
    project_one.mkdir()
    project_two.mkdir()

    monkeypatch.setattr(projects_mod, "settings", type("Settings", (), {"projects_file": projects_file})())
    projects_mod.refresh_projects()

    projects_file.write_text(
        "projects:\n"
        "  - name: one\n"
        f"    path: {project_one.as_posix()}\n"
        "    description: first\n",
        encoding="utf-8",
    )
    assert [project.name for project in projects_mod.get_projects()] == ["one"]

    projects_file.write_text(
        "projects:\n"
        "  - name: two\n"
        f"    path: {project_two.as_posix()}\n"
        "    description: second\n",
        encoding="utf-8",
    )
    assert [project.name for project in projects_mod.get_projects()] == ["two"]
