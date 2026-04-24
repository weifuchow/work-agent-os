import core.projects as projects_mod
from core.pipeline import _match_explicit_project_switch


def test_match_explicit_project_switch_prefers_riot3_frontend_alias(monkeypatch, tmp_path):
    monkeypatch.setattr(
        projects_mod,
        "get_projects",
        lambda: [
            projects_mod.ProjectConfig(
                name="allspark",
                path=tmp_path / "allspark",
                description=(
                    "Riot 调度系统 3.0（allspark）后端/调度核心。"
                    "别名：riot3、RIOT3、riot3调度系统。"
                ),
            ),
            projects_mod.ProjectConfig(
                name="riot-frontend-v3",
                path=tmp_path / "riot-frontend-v3",
                description=(
                    "RIOT3 前端项目（riot-frontend-v3）。"
                    "别名：riot3前端、riot3 前端、RIOT3前端、RIOT3 前端、"
                    "riot3 frontend、riot3-frontend、allspark frontend。"
                    "关键词线索：前端、frontend、Web UI、页面、控制台、Vue 3、Vite、Pinia。"
                ),
            ),
        ],
    )

    matched = _match_explicit_project_switch("riot3前端项目")
    assert matched is not None
    assert matched[0] == "riot-frontend-v3"

    matched = _match_explicit_project_switch("后续按 riot3 前端项目")
    assert matched is not None
    assert matched[0] == "riot-frontend-v3"


def test_match_explicit_project_switch_defaults_bare_riot3_to_allspark(monkeypatch, tmp_path):
    monkeypatch.setattr(
        projects_mod,
        "get_projects",
        lambda: [
            projects_mod.ProjectConfig(
                name="allspark",
                path=tmp_path / "allspark",
                description=(
                    "Riot 调度系统 3.0（allspark）后端/调度核心。"
                    "别名：riot3、RIOT3、riot3调度系统。"
                ),
            ),
            projects_mod.ProjectConfig(
                name="riot-frontend-v3",
                path=tmp_path / "riot-frontend-v3",
                description=(
                    "RIOT3 前端项目（riot-frontend-v3）。"
                    "别名：riot3前端、riot3 前端、RIOT3前端、RIOT3 前端、"
                    "riot3 frontend、riot3-frontend、allspark frontend。"
                ),
            ),
        ],
    )

    matched = _match_explicit_project_switch("riot3项目")
    assert matched is not None
    assert matched[0] == "allspark"

    matched = _match_explicit_project_switch("后续按 riot3 项目")
    assert matched is not None
    assert matched[0] == "allspark"
