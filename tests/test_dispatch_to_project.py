from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.fakes.dispatch_harness import (
    capture_project_agent_run,
    install_project_skill,
    make_dispatch_session,
    read_json,
    register_project,
    simulate_preflight_timeout,
    skill_defs,
    write_json,
)


def _analysis_state(path: Path, **extra: object) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    payload = {
        "project": "allspark",
        "mode": "structured",
        "phase": "keywords_ready",
        "agent_context": {
            "session_id": "triage-session-old",
            "runtime": "claude",
            "skill": "riot-log-triage",
        },
        **extra,
    }
    write_json(path / "00-state.json", payload)
    return path / "00-state.json"


@pytest.mark.asyncio
async def test_dispatch_to_project_uses_explicit_riot_log_triage_without_resuming(tmp_path, monkeypatch):
    analysis_dir = tmp_path / ".triage" / "case-1"
    state_path = _analysis_state(
        analysis_dir,
        current_question="问题时间点车辆处于什么状态、卡在哪个流程门禁",
        primary_question="为什么 AG0019 没继续出梯",
        analysis_workspace=str(analysis_dir),
        search_artifacts={"keyword_package_round1": str(analysis_dir / "keyword_package.round1.json")},
    )
    harness = await make_dispatch_session(
        tmp_path,
        monkeypatch,
        runtime="claude",
        agent_session_id="sdk-project-session",
        analysis_mode=1,
        analysis_workspace=analysis_dir,
        create_workspace=False,
    )
    register_project(
        monkeypatch,
        tmp_path,
        skills=skill_defs("ones", "riot-log-triage", "other"),
    )
    calls = capture_project_agent_run(
        monkeypatch,
        session_ids="triage-session-new",
        text="triage result",
        runtime="claude",
    )

    result = await harness.dispatch(
        project_name="allspark",
        task="请继续排查这单日志问题",
        context="请继续沿当前 triage 状态检查 CrossMapManager 日志",
        skill="riot-log-triage",
        session_id="sdk-project-session",
        db_session_id=88,
    )

    assert result["session_id"] == "triage-session-new"
    assert result["agent_session_scope"] == "record_only"
    assert calls[0]["skill"] == "riot-log-triage"
    assert calls[0]["session_id"] is None
    assert set(calls[0]["project_agents"]) == {"riot-log-triage", "other"}
    assert read_json(state_path)["agent_context"]["session_id"] == "triage-session-old"
    row = await harness.agent_run_row("SELECT agent_session_id FROM sessions WHERE id = 88")
    assert row[0] == "sdk-project-session"


@pytest.mark.asyncio
async def test_dispatch_to_project_does_not_auto_select_skill_from_stale_analysis_state(tmp_path, monkeypatch):
    analysis_dir = tmp_path / ".triage" / "case-1"
    state_path = _analysis_state(analysis_dir)
    harness = await make_dispatch_session(
        tmp_path,
        monkeypatch,
        runtime="claude",
        agent_session_id="sdk-project-session",
        analysis_mode=1,
        analysis_workspace=analysis_dir,
        create_workspace=False,
    )
    register_project(monkeypatch, tmp_path, skills=skill_defs("riot-log-triage"))
    calls = capture_project_agent_run(
        monkeypatch,
        session_ids="plain-session-new",
        text="project result",
        runtime="claude",
    )

    result = await harness.dispatch(
        project_name="allspark",
        task="普通项目问题",
        context="由主编排决定不使用 triage skill",
        session_id="sdk-project-session",
        db_session_id=88,
    )

    assert result["session_id"] == "plain-session-new"
    assert calls[0]["skill"] is None
    assert calls[0]["session_id"] is None
    assert read_json(state_path)["agent_context"]["session_id"] == "triage-session-old"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("task", "context", "uploads", "expected_skill"),
    [
        (
            "这个B-3T-1 SeerAGV 会出现，车辆直接自由导航到5001，下发的没有经过5036. 检查一下是为什么",
            "前序已上传 bootstrap.log.2026-03-25-05.2.gz 和两张截图",
            [("message-10_bootstrap.log.2026-03-25-05.2.gz", b"log"), ("message-11_media-1.png", b"\x89PNG\r\n\x1a\nimage")],
            "riot-log-triage",
        ),
        (
            "说明 allspark 的 SQLServer 配置入口",
            "普通项目配置咨询，不涉及现场日志或订单车辆执行链路",
            [],
            None,
        ),
        (
            "说明 allspark 的 SQLServer 配置入口",
            "前面有日志上传，但本轮不涉及日志，只是普通配置咨询",
            [("message-10_bootstrap.log.gz", b"log")],
            None,
        ),
    ],
)
async def test_dispatch_to_project_auto_skill_selection(
    tmp_path,
    monkeypatch,
    task,
    context,
    uploads,
    expected_skill,
):
    harness = await make_dispatch_session(tmp_path, monkeypatch)
    for name, content in uploads:
        (harness.session_dir / "uploads" / name).write_bytes(content)
    register_project(
        monkeypatch,
        tmp_path,
        skills=skill_defs("ones", "riot-log-triage", "other"),
    )
    calls = capture_project_agent_run(
        monkeypatch,
        session_ids="triage-project-session" if expected_skill else "plain-project-session",
        text="project result",
    )

    result = await harness.dispatch(
        project_name="allspark",
        task=task,
        context=context,
        db_session_id=88,
        message_id=1110,
    )
    dispatch_payload = harness.dispatch_payload(1110)

    assert calls[0]["skill"] == expected_skill
    assert result["triage_dir"] if expected_skill else result["triage_dir"] == ""
    assert dispatch_payload["auto_selected_skill"] == (expected_skill or "")
    if expected_skill:
        assert set(calls[0]["project_agents"]) == {"riot-log-triage", "other"}
        assert "必须使用 riot-log-triage workflow skill" in calls[0]["prompt"]
        assert "关联 Triage 目录" in calls[0]["prompt"]
        input_payload = read_json(Path(result["analysis_dir"]) / "input.json")
        assert input_payload["skill"] == "riot-log-triage"
        assert input_payload["triage_dir"] == result["triage_dir"]
        assert Path(result["triage_dir"], "00-state.json").is_file()
    else:
        assert "必须使用 riot-log-triage workflow skill" not in calls[0]["prompt"]
        assert dispatch_payload["skill"] == ""


@pytest.mark.asyncio
async def test_dispatch_to_project_auto_selects_triage_before_project_preflight_timeout(tmp_path, monkeypatch):
    harness = await make_dispatch_session(tmp_path, monkeypatch)
    (harness.session_dir / "uploads" / "message-10_bootstrap.log.gz").write_bytes(b"log")
    install_project_skill(tmp_path, "riot-log-triage")
    register_project(monkeypatch, tmp_path, skills=skill_defs("riot-log-triage"))

    def never_called(*args, **kwargs):
        raise AssertionError("project preparation should time out before agent run")

    from core.app import project_workspace as project_workspace_mod
    from core.orchestrator import agent_client as agent_client_mod

    monkeypatch.setattr(project_workspace_mod, "prepare_project_runtime_context", never_called)
    monkeypatch.setattr(agent_client_mod.agent_client, "run_for_project", never_called)
    monkeypatch.setattr(agent_client_mod.agent_client, "get_active_runtime", lambda: "codex")
    simulate_preflight_timeout(monkeypatch)

    result = await harness.dispatch(
        project_name="allspark",
        task="这个B-3T-1 SeerAGV 会出现，车辆直接自由导航到5001，下发的没有经过5036. 检查一下是为什么",
        context="前序已上传 bootstrap.log.gz 和截图",
        db_session_id=88,
        message_id=1110,
    )

    assert "preflight timed out" in result["error"]
    assert result["triage_dir"]
    assert read_json(Path(result["analysis_dir"]) / "input.json")["skill"] == "riot-log-triage"
    dispatch_payload = harness.dispatch_payload(1110)
    assert dispatch_payload["status"] == "preflight_timeout"
    assert dispatch_payload["skill"] == "riot-log-triage"
    assert dispatch_payload["auto_selected_skill"] == "riot-log-triage"


@pytest.mark.asyncio
async def test_dispatch_to_project_timeout_marks_failed_and_writes_artifacts(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECT_DISPATCH_TIMEOUT_SECONDS", "1")
    harness = await make_dispatch_session(tmp_path, monkeypatch)
    register_project(monkeypatch, tmp_path, skills={})

    async def slow_project_agent(**kwargs):
        await asyncio.sleep(60)
        return {"text": "too late", "session_id": "project-session"}

    from core.orchestrator import agent_client as agent_client_mod

    monkeypatch.setattr(agent_client_mod.agent_client, "get_active_runtime", lambda: "codex")
    monkeypatch.setattr(agent_client_mod.agent_client, "run_for_project", slow_project_agent)

    result = await harness.dispatch(
        project_name="allspark",
        task="分析项目",
        db_session_id=88,
        message_id=1001,
    )

    assert "timed out after 1s" in result["error"]
    assert result["agent_run_id"] == 1
    assert read_json(Path(result["result_path"]))["status"] == "timeout"
    assert "timed out after 1s" in harness.dispatch_payload(1001)["error"]
    row = await harness.agent_run_row(
        "SELECT message_id, status, output_path, error_message FROM agent_runs WHERE id = 1"
    )
    assert row[0] == 1001
    assert row[1] == "failed"
    assert row[2] == result["result_path"]
    assert "timed out after 1s" in row[3]


@pytest.mark.asyncio
async def test_dispatch_to_project_reuses_ready_workspace_without_preparing_again(tmp_path, monkeypatch):
    harness = await make_dispatch_session(tmp_path, monkeypatch)
    worktree = harness.session_dir / "worktrees" / "allspark" / "entry-1-master"
    worktree.mkdir(parents=True)
    workspace = harness.project_workspace()
    workspace.update({
        "active_project": "allspark",
        "project_order": ["allspark"],
        "projects": {
            "allspark": {
                "running_project": "allspark",
                "project_path": str(tmp_path / "repo" / "allspark"),
                "execution_path": str(worktree),
                "worktree_path": str(worktree),
                "checkout_ref": "master",
                "execution_commit_sha": "sha",
                "execution_version": "3.52.0",
                "status": "ready",
            }
        },
    })
    write_json(harness.session_dir / "project_workspace.json", workspace)
    write_json(harness.initialized.input_dir / "project_workspace.json", workspace)
    register_project(monkeypatch, tmp_path, skills={})

    def fail_prepare(*args, **kwargs):
        raise AssertionError("ready project workspace should be reused")

    from core.app import project_workspace as project_workspace_mod

    monkeypatch.setattr(project_workspace_mod, "prepare_project_from_session_workspace_path", fail_prepare)
    monkeypatch.setattr(project_workspace_mod, "prepare_project_runtime_context", fail_prepare)
    calls = capture_project_agent_run(monkeypatch, session_ids="project-session")

    result = await harness.dispatch(
        project_name="allspark",
        task="分析车辆为什么自由导航",
        context="已有 allspark session worktree",
        db_session_id=88,
        message_id=1001,
    )

    assert calls[0]["project_cwd"] == str(worktree)
    assert Path(result["analysis_dir"]).is_dir()
    assert harness.dispatch_payload(1001)["worktree_path"] == str(worktree)


@pytest.mark.asyncio
async def test_dispatch_to_project_reprepares_source_repo_marked_ready(tmp_path, monkeypatch):
    harness = await make_dispatch_session(tmp_path, monkeypatch)
    source_repo = tmp_path / "repo" / "allspark"
    source_repo.mkdir(parents=True)
    workspace = harness.project_workspace()
    workspace.update({
        "active_project": "allspark",
        "project_order": ["allspark"],
        "projects": {
            "allspark": {
                "running_project": "allspark",
                "project_path": str(source_repo),
                "source_path": str(source_repo),
                "execution_path": str(source_repo),
                "worktree_path": str(source_repo),
                "checkout_ref": "",
                "status": "ready",
            }
        },
    })
    write_json(harness.session_dir / "project_workspace.json", workspace)
    write_json(harness.initialized.input_dir / "project_workspace.json", workspace)
    register_project(monkeypatch, tmp_path, skills={})
    calls = capture_project_agent_run(monkeypatch, session_ids="project-session")

    result = await harness.dispatch(
        project_name="allspark",
        task="分析现场日志",
        context="已有 registry 误把源仓库标成 ready",
        db_session_id=88,
        message_id=1001,
    )

    expected_worktree = harness.session_dir / "worktrees" / "allspark" / "stable-main"
    assert calls[0]["project_cwd"] == str(expected_worktree)
    assert harness.dispatch_payload(1001)["worktree_path"] == str(expected_worktree)
    assert not result.get("error")


@pytest.mark.asyncio
async def test_dispatch_to_project_preflight_timeout_keeps_artifacts(tmp_path, monkeypatch):
    harness = await make_dispatch_session(tmp_path, monkeypatch)
    register_project(monkeypatch, tmp_path, skills=skill_defs("riot-log-triage"))

    def never_called(*args, **kwargs):
        raise AssertionError("project agent should not run after preflight timeout")

    from core.app import project_workspace as project_workspace_mod
    from core.orchestrator import agent_client as agent_client_mod

    monkeypatch.setattr(project_workspace_mod, "prepare_project_runtime_context", never_called)
    monkeypatch.setattr(agent_client_mod.agent_client, "run_for_project", never_called)
    monkeypatch.setattr(agent_client_mod.agent_client, "get_active_runtime", lambda: "codex")
    simulate_preflight_timeout(monkeypatch)

    result = await harness.dispatch(
        project_name="allspark",
        task="检查 B-3T-1 SeerAGV 为什么直接自由导航到 5001",
        context="前序已上传日志和截图",
        skill="riot-log-triage",
        db_session_id=88,
        message_id=1001,
    )

    analysis_dir = Path(result["analysis_dir"])
    triage_dir = Path(result["triage_dir"])
    assert "preflight timed out" in result["error"]
    assert (analysis_dir / "input.json").is_file()
    assert (analysis_dir / "result.json").is_file()
    assert (analysis_dir / "analysis_trace.md").is_file()
    assert (triage_dir / "00-state.json").is_file()
    assert (triage_dir / "01-intake" / "messages" / "dispatch_input.json").is_file()
    assert harness.dispatch_payload(1001)["status"] == "preflight_timeout"
    row = await harness.agent_run_row(
        "SELECT status, input_path, output_path, error_message FROM agent_runs WHERE id = 1"
    )
    assert row[0] == "failed"
    assert row[1].endswith("input.json")
    assert row[2].endswith("result.json")
    assert "preflight timed out" in row[3]


@pytest.mark.asyncio
async def test_dispatch_to_project_passes_session_upload_images_to_project_agent(tmp_path, monkeypatch):
    harness = await make_dispatch_session(tmp_path, monkeypatch)
    image_path = harness.session_dir / "uploads" / "message-10_media-1.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nimage")
    (harness.session_dir / "uploads" / "message-11_log.txt").write_text("not image", encoding="utf-8")
    register_project(monkeypatch, tmp_path, skills={})
    calls = capture_project_agent_run(monkeypatch, session_ids="project-session")

    result = await harness.dispatch(
        project_name="allspark",
        task="结合前序截图分析 SeerAGV 执行任务错误",
        context="当前消息没有新图片，但同 session 前序有截图",
        db_session_id=88,
        message_id=1001,
    )

    expected_image = str(image_path.resolve())
    assert calls[0]["image_paths"] == [expected_image]
    assert expected_image in calls[0]["prompt"]
    assert read_json(Path(result["analysis_dir"]) / "input.json")["image_paths"] == [expected_image]
    assert harness.dispatch_payload(1001)["image_paths"] == [expected_image]


@pytest.mark.asyncio
async def test_dispatch_to_project_keeps_agent_sessions_project_scoped(tmp_path, monkeypatch):
    harness = await make_dispatch_session(tmp_path, monkeypatch, project="")
    register_project(
        monkeypatch,
        tmp_path,
        projects=("allspark", "riot-frontend-v3"),
        skills={},
    )
    calls = capture_project_agent_run(
        monkeypatch,
        session_ids=["allspark-session-1", "frontend-session-1", "allspark-session-2"],
    )

    first = await harness.dispatch(
        project_name="allspark",
        task="分析后端",
        session_id="orchestrator-session",
        db_session_id=88,
    )
    second = await harness.dispatch(
        project_name="riot-frontend-v3",
        task="分析前端",
        session_id="orchestrator-session",
        db_session_id=88,
    )
    third = await harness.dispatch(
        project_name="allspark",
        task="继续分析后端",
        db_session_id=88,
    )

    assert [call["session_id"] for call in calls] == [None, None, None]
    assert first["resume_source"] == second["resume_source"] == third["resume_source"] == ""
    payload = harness.project_workspace()
    assert payload["agent_sessions"]["projects"] == {}
    assert [item["agent_session_id"] for item in payload["project_agent_runs"]["allspark"]] == [
        "allspark-session-1",
        "allspark-session-2",
    ]
    assert payload["project_agent_runs"]["riot-frontend-v3"][0]["agent_session_id"] == "frontend-session-1"
    assert Path(first["analysis_dir"]).is_dir()
    assert Path(first["result_path"]).is_file()
    assert Path(second["analysis_dir"]).is_dir()
    assert Path(third["analysis_dir"]).is_dir()
    row = await harness.agent_run_row("SELECT agent_session_id, project FROM sessions WHERE id = 88")
    assert row == ("orchestrator-session", "allspark")


@pytest.mark.asyncio
async def test_dispatch_to_project_accepts_explicit_riot_log_triage_skill(tmp_path, monkeypatch):
    harness = await make_dispatch_session(tmp_path, monkeypatch)
    register_project(
        monkeypatch,
        tmp_path,
        skills=skill_defs("ones", "riot-log-triage", "other"),
    )
    calls = capture_project_agent_run(
        monkeypatch,
        session_ids="triage-project-session",
        text="triage result",
    )

    result = await harness.dispatch(
        project_name="allspark",
        task="分析 ONES 工单 #150745 的订单执行问题",
        context="ONES 工单 + 现场日志排障",
        skill="riot-log-triage",
        db_session_id=88,
    )

    assert result["session_id"] == "triage-project-session"
    assert calls[0]["skill"] == "riot-log-triage"
    assert set(calls[0]["project_agents"]) == {"riot-log-triage", "other"}
    assert "必须使用 riot-log-triage workflow skill" in calls[0]["prompt"]
    saved = harness.project_workspace()["project_agent_runs"]["allspark"][0]
    assert saved["agent_session_id"] == "triage-project-session"
    assert saved["skill"] == "riot-log-triage"
    assert saved["status"] == "success"
    assert read_json(Path(result["result_path"]))["project_agent_session_record_only"] is True
    assert read_json(Path(result["analysis_dir"]) / "input.json")["skill"] == "riot-log-triage"


@pytest.mark.asyncio
async def test_dispatch_to_project_does_not_resume_plain_project_session_for_requested_skill(tmp_path, monkeypatch):
    harness = await make_dispatch_session(tmp_path, monkeypatch)
    register_project(monkeypatch, tmp_path, skills=skill_defs("riot-log-triage"))
    calls = capture_project_agent_run(
        monkeypatch,
        session_ids=["plain-project-session", "triage-project-session"],
    )

    await harness.dispatch(project_name="allspark", task="普通项目咨询", db_session_id=88)
    await harness.dispatch(
        project_name="allspark",
        task="分析日志排障",
        skill="riot-log-triage",
        db_session_id=88,
    )

    assert calls[0]["session_id"] is None
    assert calls[0]["skill"] is None
    assert calls[1]["session_id"] is None
    assert calls[1]["skill"] == "riot-log-triage"


@pytest.mark.asyncio
async def test_dispatch_to_project_uses_session_agent_runtime_when_contextvar_is_default(tmp_path, monkeypatch):
    harness = await make_dispatch_session(tmp_path, monkeypatch, runtime="codex")
    register_project(monkeypatch, tmp_path, skills={})
    calls = capture_project_agent_run(monkeypatch, session_ids="project-session", runtime="claude")

    result = await harness.dispatch(project_name="allspark", task="分析项目", db_session_id=88)

    assert result["session_id"] == "project-session"
    assert calls[0]["runtime"] == "codex"


@pytest.mark.asyncio
async def test_dispatch_to_project_marks_agent_runtime_error_failed(tmp_path, monkeypatch):
    harness = await make_dispatch_session(tmp_path, monkeypatch, runtime="codex")
    register_project(monkeypatch, tmp_path, skills={})
    capture_project_agent_run(
        monkeypatch,
        session_ids="project-session",
        text="Failed to authenticate",
        is_error=True,
        runtime="claude",
    )

    result = await harness.dispatch(project_name="allspark", task="分析项目", db_session_id=88)

    assert result["error"] == "Failed to authenticate"
    row = await harness.agent_run_row(
        "SELECT status, error_message FROM agent_runs WHERE session_id = 88 AND agent_name = 'dispatch:allspark'"
    )
    assert row == ("failed", "Failed to authenticate")


@pytest.mark.asyncio
async def test_dispatch_to_project_persists_analysis_without_db_session(tmp_path, monkeypatch):
    from core.orchestrator import agent_client as agent_client_mod
    import core.projects as projects_mod

    monkeypatch.setattr(agent_client_mod, "PROJECT_ROOT", tmp_path)
    project = SimpleNamespace(name="allspark", path=tmp_path / "repo" / "allspark", description="backend")
    project.path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(projects_mod, "get_project", lambda name: project if name == "allspark" else None)
    monkeypatch.setattr(projects_mod, "merge_skills", lambda *args, **kwargs: {})

    def fake_prepare_project_runtime_context(project_name, ones_result=None, worktree_root=None, worktree_token=""):
        execution_path = tmp_path / "repo" / project_name
        return SimpleNamespace(
            execution_path=execution_path,
            to_payload=lambda: {
                "running_project": project_name,
                "project_path": str(project.path),
                "execution_path": str(execution_path),
            },
        )

    monkeypatch.setattr(projects_mod, "prepare_project_runtime_context", fake_prepare_project_runtime_context)
    capture_project_agent_run(monkeypatch, session_ids="record-only-session")

    result = await agent_client_mod.dispatch_to_project.handler({
        "project_name": "allspark",
        "task": "分析项目",
        "message_id": 1001,
    })

    analysis_dir = Path(result["analysis_dir"])
    assert analysis_dir == tmp_path / "data" / "sessions" / "no-session" / ".analysis" / "message-1001" / "allspark" / "dispatch-untracked"
    assert (analysis_dir / "input.json").is_file()
    assert (analysis_dir / "prompt.md").is_file()
    assert (analysis_dir / "result.json").is_file()
    assert result["agent_session_scope"] == "record_only"
