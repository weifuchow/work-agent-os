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


def test_git_command_timeout_kills_windows_process_tree(monkeypatch, tmp_path):
    calls = []

    class FakeProc:
        pid = 12345
        returncode = None

        def communicate(self, timeout=None):
            calls.append(("communicate", timeout))
            if timeout == 5:
                raise projects_mod.subprocess.TimeoutExpired(["git"], timeout)
            self.returncode = -9
            return "", "timed out"

    def fake_popen(*args, **kwargs):
        calls.append(("popen", args, kwargs))
        return FakeProc()

    def fake_run(cmd, **kwargs):
        calls.append(("run", cmd, kwargs))
        return projects_mod.subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(projects_mod.os, "name", "nt")
    monkeypatch.setattr(projects_mod.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(projects_mod.subprocess, "run", fake_run)

    assert projects_mod._git_capture(tmp_path, "rev-parse", "--abbrev-ref", "HEAD") == ""
    assert any(call[0] == "run" and call[1][:2] == ["taskkill", "/PID"] for call in calls)
