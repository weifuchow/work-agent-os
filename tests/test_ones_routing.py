from types import SimpleNamespace
import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys

import core.projects as projects_mod


def _load_ones_routing_module():
    path = Path(__file__).resolve().parents[1] / ".claude" / "skills" / "ones" / "scripts" / "routing.py"
    spec = importlib.util.spec_from_file_location("ones_skill_routing", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ones_routing_mod = _load_ones_routing_module()
choose_project_route = ones_routing_mod.choose_project_route
extract_ones_task_link = ones_routing_mod.extract_ones_task_link
score_project_routes = ones_routing_mod.score_project_routes

ONES_150552_DIRECT_MESSAGE = (
    "#150552 【3.52.0】【订单】：订单取消成功，但是订单未与小车解绑"
    "（现象，小车挂着订单，同时在执行其他订单）\n"
    "https://ones.standard-robots.com:10120/project/#/team/UNrQ5Ny5/task/NbJXtiyGP7R4vYnF"
)


def test_extract_ones_task_link_parses_team_and_ref():
    team, ref, url = extract_ones_task_link(
        "请看这个工单 https://ones.standard-robots.com:10120/project/#/team/UNrQ5Ny5/task/1FmsdpJjHT3JPyWL"
    )
    assert team == "UNrQ5Ny5"
    assert ref == "1FmsdpJjHT3JPyWL"
    assert url.endswith("/task/1FmsdpJjHT3JPyWL")


def test_extract_ones_task_link_normalizes_component_view_url():
    team, ref, url = extract_ones_task_link(
        "https://ones.standard-robots.com:10120/project/#/team/UNrQ5Ny5/"
        "project/UEjcYfJhPshGIUnd/component/OORdUJMu/view/H5TRzRtB/task/NbJXtiyGP7R4vYnF"
    )

    assert team == "UNrQ5Ny5"
    assert ref == "NbJXtiyGP7R4vYnF"
    assert url == "https://ones.standard-robots.com:10120/project/#/team/UNrQ5Ny5/task/NbJXtiyGP7R4vYnF"


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
    assert any("3.51.0" in reason for reason in reasons)


def test_direct_title_and_link_triggers_ones_and_routes_riot3_order_issue():
    team, ref, url = extract_ones_task_link(ONES_150552_DIRECT_MESSAGE)
    assert team == "UNrQ5Ny5"
    assert ref == "NbJXtiyGP7R4vYnF"
    assert url == "https://ones.standard-robots.com:10120/project/#/team/UNrQ5Ny5/task/NbJXtiyGP7R4vYnF"

    project_name, confidence, score, reasons = choose_project_route(
        user_message=ONES_150552_DIRECT_MESSAGE,
        task_summary="",
        task_description="",
        ones_project_name="",
        business_project_name="",
        key_fields={},
    )

    assert project_name == "allspark"
    assert confidence == "medium"
    assert score >= 4
    assert reasons
    assert all("3.52.0" in reason and reason.endswith("-> allspark") for reason in reasons)


def test_choose_project_route_prefers_version_hint_over_generic_charge_keywords():
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
    assert project_name == "fms-java"
    assert confidence in {"medium", "high"}
    assert any("4.9.2-186-g96fb6f2f9_20250723" in reason for reason in reasons)


def test_choose_project_route_ignores_iteration_and_uses_business_version_signal():
    project_name, confidence, score, reasons = choose_project_route(
        user_message="请分析这个 ONES 链接",
        task_summary="【KIOXIA岩手工厂日本自动搬运复购项目】无法生成自动充电任务",
        task_description="调度上手动下发快速充电任务显示没有可用充电桩。",
        ones_project_name="软件部-基础迭代组",
        business_project_name="KIOXIA岩手工厂日本自动搬运复购项目",
        key_fields={
            "FMS/RIoT版本": "4.9.2-186-g96fb6f2f9_20250723",
            "一级问题根因分类": "FMS/调度",
            "产品名称-底盘/软件": ["软件产品-调度"],
        },
    )
    assert project_name == "fms-java"
    assert confidence in {"medium", "high"}
    assert score >= 4
    assert any("4.9.2-186-g96fb6f2f9_20250723" in reason for reason in reasons)


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


def test_choose_project_route_prefers_riot3_frontend_over_allspark_backend():
    project_name, confidence, score, reasons = choose_project_route(
        user_message="帮我看 riot3 前端白屏问题",
        task_summary="RIOT3 前端页面异常",
        task_description="riot3 frontend 登录页白屏，vite build 后页面加载失败。",
        ones_project_name="",
        business_project_name="",
        key_fields={"FMS/RIoT版本": "3.51.0"},
    )
    assert project_name == "riot-frontend-v3"
    assert confidence in {"medium", "high"}
    assert score > 8
    assert any("前端" in reason or "frontend" in reason for reason in reasons)


def test_choose_project_route_defaults_bare_riot3_to_allspark():
    project_name, confidence, score, reasons = choose_project_route(
        user_message="帮我看 riot3 的问题",
        task_summary="RIOT3 调度异常",
        task_description="现场反馈 riot3 出现调度卡顿。",
        ones_project_name="",
        business_project_name="",
        key_fields={"FMS/RIoT版本": "3.51.0"},
    )
    assert project_name == "allspark"
    assert confidence in {"medium", "high"}
    assert score >= 8
    assert all("frontend" not in reason.lower() for reason in reasons)


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


def test_resolve_project_runtime_context_prefers_release_branch_and_exact_tag(monkeypatch, tmp_path):
    monkeypatch.setattr(
        projects_mod,
        "get_project",
        lambda name: projects_mod.ProjectConfig(name=name, path=tmp_path, description=""),
    )
    monkeypatch.setattr(
        projects_mod,
        "get_project_git_meta",
        lambda **kwargs: projects_mod.ProjectGitMeta(
            branch="master",
            commit_sha="abc123456789",
            describe="3.50.0-12-gabc12345",
            version_hint="3.50.0",
        ),
    )
    monkeypatch.setattr(
        projects_mod,
        "_git_ref_inventory",
        lambda path_str: {
            "local_branches": ("master",),
            "remote_branches": ("origin/release/3.50.x",),
            "tags": ("3.50.4",),
        },
    )

    ctx = projects_mod.resolve_project_runtime_context(
        "allspark",
        ones_result={
            "task": {"number": 149999},
            "named_fields": {"FMS/RIoT版本": "3.50.4-12-gabcdef"},
            "project": {"display_name": "示例现场项目"},
        },
    )

    assert ctx is not None
    assert ctx.target_branch == "release/3.50.x"
    assert ctx.target_branch_ref == "origin/release/3.50.x"
    assert ctx.target_tag == "3.50.4"
    assert ctx.checkout_ref == "3.50.4"
    assert ctx.business_project_name == "示例现场项目"


def test_resolve_project_runtime_context_falls_back_to_master_when_no_version_branch(monkeypatch, tmp_path):
    monkeypatch.setattr(
        projects_mod,
        "get_project",
        lambda name: projects_mod.ProjectConfig(name=name, path=tmp_path, description=""),
    )
    monkeypatch.setattr(
        projects_mod,
        "get_project_git_meta",
        lambda **kwargs: projects_mod.ProjectGitMeta(
            branch="dev",
            commit_sha="3cf57f157abc",
            describe="dev",
            version_hint="dev@3cf57f15",
        ),
    )
    monkeypatch.setattr(
        projects_mod,
        "_git_ref_inventory",
        lambda path_str: {
            "local_branches": ("dev",),
            "remote_branches": ("origin/master",),
            "tags": ("v4.9.2",),
        },
    )

    ctx = projects_mod.resolve_project_runtime_context(
        "fms-java",
        ones_result={
            "task": {"uuid": "task-uuid-001"},
            "named_fields": {"FMS/RIoT版本": "4.9.2-186-g96fb6f2f9_20250723"},
            "project": {"display_name": "KIOXIA岩手工厂日本自动搬运复购项目"},
        },
    )

    assert ctx is not None
    assert ctx.target_branch == "master"
    assert ctx.target_branch_ref == "origin/master"
    assert ctx.target_tag == "v4.9.2"
    assert ctx.checkout_ref == "v4.9.2"
    assert ctx.version_source_field == "FMS/RIoT版本"
    assert ctx.version_source_value == "4.9.2-186-g96fb6f2f9_20250723"


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def test_prepare_project_runtime_context_creates_worktree_for_matching_tag(monkeypatch, tmp_path):
    repo = tmp_path / "fms-java"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "README.md").write_text("demo\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")
    _git(repo, "tag", "v4.9.2")

    monkeypatch.setattr(
        projects_mod,
        "get_project",
        lambda name: projects_mod.ProjectConfig(name=name, path=repo, description=""),
    )
    monkeypatch.setattr(projects_mod, "settings", SimpleNamespace(project_root=tmp_path))
    projects_mod._git_ref_inventory.cache_clear()

    ctx = projects_mod.prepare_project_runtime_context(
        "fms-java",
        ones_result={
            "task": {"number": 149268},
            "named_fields": {"FMS/RIoT版本": "4.9.2-186-g96fb6f2f9_20250723"},
            "project": {"display_name": "KIOXIA岩手工厂日本自动搬运复购项目"},
        },
    )

    assert ctx is not None
    assert ctx.execution_path != repo
    assert ctx.execution_path.exists()
    assert ctx.target_tag == "v4.9.2"
    assert ctx.checkout_ref == "v4.9.2"
    current_ref = _git(ctx.execution_path, "describe", "--tags", "--exact-match", "HEAD").stdout.strip()
    assert current_ref == "v4.9.2"
    assert any("已创建 worktree" in note or "复用已有 worktree" in note for note in ctx.notes)


def test_prepare_project_runtime_context_reuses_legacy_issue_version_worktree(monkeypatch, tmp_path):
    repo = tmp_path / "fms-java"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "README.md").write_text("demo\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")
    _git(repo, "tag", "v4.9.2")

    legacy_path = tmp_path / ".worktrees" / "fms-java" / "149268-4.9.2"
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", "--detach", str(legacy_path), "v4.9.2")

    monkeypatch.setattr(
        projects_mod,
        "get_project",
        lambda name: projects_mod.ProjectConfig(name=name, path=repo, description=""),
    )
    monkeypatch.setattr(projects_mod, "settings", SimpleNamespace(project_root=tmp_path))
    projects_mod._git_ref_inventory.cache_clear()

    ctx = projects_mod.prepare_project_runtime_context(
        "fms-java",
        ones_result={
            "task": {"number": 149268, "uuid": "1FmsdpJjHT3JPyWL"},
            "named_fields": {"FMS/RIoT版本": "4.9.2-186-g96fb6f2f9_20250723"},
            "project": {"display_name": "KIOXIA岩手工厂日本自动搬运复购项目"},
        },
    )

    assert ctx is not None
    assert ctx.execution_path == legacy_path
    assert not (tmp_path / ".worktrees" / "fms-java" / "149268-1FmsdpJjHT3JPyWL-4.9.2").exists()
    assert any("复用已有 worktree" in note for note in ctx.notes)


def test_prepare_project_runtime_context_recovers_missing_registered_worktree(monkeypatch, tmp_path):
    repo = tmp_path / "fms-java"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "README.md").write_text("demo\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")
    _git(repo, "tag", "v4.9.2")

    stale_path = tmp_path / ".worktrees" / "fms-java" / "149268-1FmsdpJjHT3JPyWL-4.9.2"
    stale_path.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", "--detach", str(stale_path), "v4.9.2")
    shutil.rmtree(stale_path)

    monkeypatch.setattr(
        projects_mod,
        "get_project",
        lambda name: projects_mod.ProjectConfig(name=name, path=repo, description=""),
    )
    monkeypatch.setattr(projects_mod, "settings", SimpleNamespace(project_root=tmp_path))
    projects_mod._git_ref_inventory.cache_clear()

    ctx = projects_mod.prepare_project_runtime_context(
        "fms-java",
        ones_result={
            "task": {"number": 149268, "uuid": "1FmsdpJjHT3JPyWL"},
            "named_fields": {"FMS/RIoT版本": "4.9.2-186-g96fb6f2f9_20250723"},
            "project": {"display_name": "KIOXIA岩手工厂日本自动搬运复购项目"},
        },
    )

    assert ctx is not None
    assert ctx.execution_path == stale_path
    assert ctx.execution_path.exists()
    assert any("清理失效 worktree 注册" in note for note in ctx.notes)
    assert any("已创建 worktree" in note for note in ctx.notes)


def test_prepare_project_runtime_context_uses_backup_path_when_preferred_creation_fails(monkeypatch, tmp_path):
    repo = tmp_path / "fms-java"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "README.md").write_text("demo\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")
    _git(repo, "tag", "v4.9.2")

    monkeypatch.setattr(
        projects_mod,
        "get_project",
        lambda name: projects_mod.ProjectConfig(name=name, path=repo, description=""),
    )
    monkeypatch.setattr(projects_mod, "settings", SimpleNamespace(project_root=tmp_path))
    projects_mod._git_ref_inventory.cache_clear()

    original_create = projects_mod._create_detached_worktree

    def flaky_create(project_path, worktree_path, ref, *, force=False):
        if worktree_path.name == "149268-1FmsdpJjHT3JPyWL-4.9.2" and not force:
            return False, "fatal: could not create directory of '.git/worktrees/149268-1FmsdpJjHT3JPyWL-4.9.2': Permission denied"
        return original_create(project_path, worktree_path, ref, force=force)

    monkeypatch.setattr(projects_mod, "_create_detached_worktree", flaky_create)

    ctx = projects_mod.prepare_project_runtime_context(
        "fms-java",
        ones_result={
            "task": {"number": 149268, "uuid": "1FmsdpJjHT3JPyWL"},
            "named_fields": {"FMS/RIoT版本": "4.9.2-186-g96fb6f2f9_20250723"},
            "project": {"display_name": "KIOXIA岩手工厂日本自动搬运复购项目"},
        },
    )

    assert ctx is not None
    assert ctx.execution_path != repo
    assert ctx.execution_path.name == "149268-4.9.2"
    assert ctx.execution_path.exists()
    assert any("首选 worktree 路径不可用" in note for note in ctx.notes)
