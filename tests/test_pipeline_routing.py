import core.projects as projects_mod
from core.pipeline import _build_riot_log_seed_keyword_package, _match_explicit_project_switch


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


def test_riot_log_seed_package_routes_elevator_cross_map_order_execution():
    package = _build_riot_log_seed_keyword_package(
        project_name="allspark",
        issue_type="order_execution",
        vehicle_name="AG0019",
        order_id="",
        text=(
            "RIot3.46.23 小车 AG0019 在 2026-04-21 18:34 乘梯从 4 楼去往 5 楼，"
            "切换地图后一直在电梯里面不出来，十多分钟后才开始移动。"
        ),
        normalized_window={
            "start": "2026-04-21 10:04:00",
            "end": "2026-04-21 11:04:00",
        },
    )

    assert package["anchor_terms"] == ["AG0019"]
    assert "ChangeMapRequest" in package["gate_terms"]
    assert "MoveRequest" in package["gate_terms"]
    assert "CrossMapManager" in package["core_terms"]
    assert "ElevatorResourceDevice" in package["core_terms"]
    assert "vehicleProcState" in package["stage_terms"]
    assert "IN_CHANGE_MAP" in package["stage_terms"]
    assert "bootstrap" in package["target_files"]
    assert "reservation" in package["target_files"]
    assert "notify" in package["target_files"]
    assert package["excluded_files"] == ["metric"]
    assert package["preferred_files"][0] == "bootstrap"
    assert package["anchor_match_mode"] == "prefer"
    assert package["require_anchor"] is False
    assert package["require_gate_when_present"] is False
    assert '"AG0019"' in package["dsl_query"]
    assert '"ChangeMapRequest"' in package["dsl_query"]
    assert " OR " in package["dsl_query"]
    assert any(item["term"] == "CrossMapManager" for item in package["term_priorities"])
