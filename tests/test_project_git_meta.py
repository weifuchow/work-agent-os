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
