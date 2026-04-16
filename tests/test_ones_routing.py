from types import SimpleNamespace

import core.ones_routing as ones_routing_mod
from core.ones_routing import choose_project_route, extract_ones_task_link, score_project_routes


def test_extract_ones_task_link_parses_team_and_ref():
    team, ref, url = extract_ones_task_link(
        "请看这个工单 https://ones.standard-robots.com:10120/project/#/team/UNrQ5Ny5/task/1FmsdpJjHT3JPyWL"
    )
    assert team == "UNrQ5Ny5"
    assert ref == "1FmsdpJjHT3JPyWL"
    assert url.endswith("/task/1FmsdpJjHT3JPyWL")


def test_choose_project_route_uses_version_major_as_hint():
    project_name, confidence, score, reasons = choose_project_route(
        user_message="帮我看这个工单",
        task_summary="版本兼容问题",
        task_description="",
        ones_project_name="",
        business_project_name="",
        key_fields={"FMS/RIoT版本": "3.51.0"},
    )
    assert project_name == "allspark"
    assert confidence in {"medium", "high"}
    assert score >= 4
    assert any("3.x" in reason for reason in reasons)


def test_choose_project_route_prefers_dispatch_domain_over_4x_version_hint():
    project_name, confidence, score, reasons = choose_project_route(
        user_message="请分析这个 ONES 链接",
        task_summary="【KIOXIA岩手工厂日本自动搬运复购项目】无法生成自动充电任务",
        task_description=(
            "调度上手动下发快速充电任务显示没有可用充电桩，"
            "执行一次单机导航后自动生成充电任务。"
        ),
        ones_project_name="问题管理中心",
        business_project_name="KIOXIA岩手工厂日本自动搬运复购项目",
        key_fields={"FMS/RIoT版本": "4.9.2-186-g96fb6f2f9_20250723"},
    )
    assert project_name == "allspark"
    assert confidence in {"medium", "high"}
    assert any("调度" in reason or "充电任务" in reason for reason in reasons)


def test_score_project_routes_can_identify_deployment_package():
    scored = score_project_routes(
        user_message="这个部署包 docker 镜像安装失败",
        task_summary="RIOT3 安装包问题",
        task_description="podman 启动 supervisord 异常",
        ones_project_name="",
        business_project_name="",
        key_fields={},
    )
    assert scored["allsparkbox"]["score"] > scored["allspark"]["score"]


def test_read_env_fallback_supports_dot_ones_env(monkeypatch, tmp_path):
    env_path = tmp_path / ".ones.env"
    env_path.write_text(
        "ONES_HOST=https://ones.standard-robots.com:10120\nONES_TEAM_UUID=UNrQ5Ny5\n",
        encoding="utf-8",
    )

    monkeypatch.delenv("ONES_ENV_FILE", raising=False)
    monkeypatch.setattr(ones_routing_mod, "settings", SimpleNamespace(project_root=tmp_path))
    ones_routing_mod._read_env_fallback.cache_clear()

    values = ones_routing_mod._read_env_fallback()

    assert values["ONES_HOST"] == "https://ones.standard-robots.com:10120"
    assert values["ONES_TEAM_UUID"] == "UNrQ5Ny5"

    ones_routing_mod._read_env_fallback.cache_clear()
