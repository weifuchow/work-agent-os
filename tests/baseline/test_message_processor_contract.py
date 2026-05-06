from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import aiosqlite
import pytest

ROOT = Path(__file__).resolve().parents[2]
TESTS_ROOT = ROOT / "tests"
for item in (ROOT, TESTS_ROOT):
    if str(item) not in sys.path:
        sys.path.insert(0, str(item))

from core.agents.runner import AgentRunner
from core.app.message_processor import MessageProcessorDeps
from core.app.result_handler import ResultHandler
from core.artifacts.workspace import WorkspacePreparer
from core.deps import CoreDependencies
from core.pipeline import configure_dependencies_for_tests, process_message, reprocess_message
from core.ports import AgentRequest, AgentResponse
from core.projects import ProjectConfig
import core.projects as projects_mod
from core.repositories import Repository
from core.sessions.service import SessionService
from builders.messages import create_schema, fetch_all, fetch_one, insert_message
from fakes.ports import FakeAgentPort, FakeChannelPort, FakeFilePort, FixedClock, read_workspace_json


@pytest.fixture
async def harness(tmp_path):
    db_file = tmp_path / "baseline.sqlite"
    await create_schema(db_file)

    repository = Repository(db_file)
    sessions = SessionService(repository)
    clock = FixedClock()
    channel = FakeChannelPort()
    file_port = FakeFilePort()
    agent_port = FakeAgentPort(_reply_result("hello from skill"))
    workspaces = WorkspacePreparer(file_port, workspace_root=tmp_path / "workspaces")
    agents = AgentRunner(agent_port)
    result_handler = ResultHandler(
        repository=repository,
        channel_port=channel,
        clock=clock,
        reply_repairer=agents,
    )
    deps = CoreDependencies(
        repository=repository,
        sessions=sessions,
        workspaces=workspaces,
        agents=agents,
        result_handler=result_handler,
        clock=clock,
    )
    configure_dependencies_for_tests(deps)
    try:
        yield {
            "db_file": db_file,
            "deps": deps,
            "agent_port": agent_port,
            "channel": channel,
            "file_port": file_port,
            "workspace_root": tmp_path / "workspaces",
        }
    finally:
        configure_dependencies_for_tests(None)


@pytest.mark.asyncio
async def test_reply_flow_completes_and_delivers_payload(harness):
    db_file = harness["db_file"]
    msg_id = await insert_message(db_file, content="早上好")

    await process_message(msg_id)

    msg = await fetch_one(db_file, "SELECT * FROM messages WHERE id = ?", (msg_id,))
    assert msg["pipeline_status"] == "completed"
    assert msg["session_id"] is not None

    assert len(harness["agent_port"].calls) == 1
    workspace = harness["agent_port"].calls[0].workspace_path
    assert read_workspace_json(workspace, "input/message.json")["content"] == "早上好"
    assert "skills" in read_workspace_json(workspace, "input/skill_registry.json")
    artifact_roots = read_workspace_json(workspace, "input/artifact_roots.json")
    project_context = read_workspace_json(workspace, "input/project_context.json")
    project_workspace = read_workspace_json(workspace, "input/project_workspace.json")
    session_dir = Path(artifact_roots["session_dir"])
    session_workspace_path = session_dir / "session_workspace.json"
    assert not (workspace / "input" / "session_workspace.json").exists()
    session_workspace = json.loads(session_workspace_path.read_text(encoding="utf-8"))
    assert session_dir.name == "session-1"
    assert Path(artifact_roots["workspace_dir"]) == workspace.resolve()
    assert session_workspace["session_dir"] == artifact_roots["session_dir"]
    assert workspace == session_dir / "workspace"
    assert session_workspace["workspace_dir"] == str((session_dir / "workspace").resolve())
    assert session_workspace["session_artifact_roots"] == artifact_roots
    assert session_workspace["write_policy"]["final_reply_contract"] == "workspace/output"
    assert "data/attachments" in session_workspace["forbidden_roots"]
    assert project_context["artifact_roots"] == artifact_roots
    assert project_context["session_workspace_path"] == str(session_workspace_path.resolve())
    assert project_context["project_workspace_path"] == str((session_dir / "project_workspace.json").resolve())
    assert project_context["project_workspace"]["projects"] == {}
    assert project_workspace["projects"] == {}
    assert project_workspace["worktrees_dir"] == artifact_roots["worktrees_dir"]
    for key in ("ones_dir", "triage_dir", "review_dir", "uploads_dir", "attachments_dir", "scratch_dir"):
        path = Path(artifact_roots[key])
        assert path.is_dir()
        assert path.resolve().is_relative_to(session_dir.resolve())

    assert len(harness["channel"].calls) == 1
    assert harness["channel"].calls[0]["reply"].type == "markdown"
    assert harness["channel"].calls[0]["reply"].content == "hello from skill"

    reply = await fetch_one(
        db_file,
        "SELECT * FROM messages WHERE platform_message_id = ?",
        ("reply_om_test_001",),
    )
    assert reply["content"] == "hello from skill"

    audits = await fetch_all(db_file, "SELECT event_type FROM audit_logs ORDER BY id")
    assert [item["event_type"] for item in audits] == [
        "message_processing_started",
        "workspace_prepared",
        "agent_run_started",
        "agent_run_completed",
        "reply_delivery_started",
        "message_completed",
    ]
    workspace_audit = await fetch_one(
        db_file,
        "SELECT detail FROM audit_logs WHERE event_type = ?",
        ("workspace_prepared",),
    )
    workspace_detail = json.loads(workspace_audit["detail"])
    assert workspace_detail["session_dir"] == artifact_roots["session_dir"]
    assert Path(workspace_detail["session_workspace_path"]) == session_workspace_path
    assert workspace_detail["artifact_roots"] == artifact_roots

    completed_audit = await fetch_one(
        db_file,
        "SELECT detail FROM audit_logs WHERE event_type = ?",
        ("message_completed",),
    )
    completed_detail = json.loads(completed_audit["detail"])
    assert completed_detail["reply_message_id"] == "reply_om_test_001"
    assert completed_detail["reply_thread_id"] == "omt_reply_001"
    assert completed_detail["reply_root_id"] == "om_root_001"


@pytest.mark.asyncio
async def test_thread_message_reuses_existing_session(harness):
    db_file = harness["db_file"]
    first_id = await insert_message(db_file, platform_message_id="om_first", content="第一轮")
    await process_message(first_id)
    first = await fetch_one(db_file, "SELECT * FROM messages WHERE id = ?", (first_id,))

    second_id = await insert_message(
        db_file,
        platform_message_id="om_second",
        content="继续",
        thread_id="omt_reply_001",
    )
    harness["agent_port"].results.append(_reply_result("continued"))
    await process_message(second_id)
    second = await fetch_one(db_file, "SELECT * FROM messages WHERE id = ?", (second_id,))

    assert second["pipeline_status"] == "completed"
    assert second["session_id"] == first["session_id"]


@pytest.mark.asyncio
async def test_feishu_thread_lookup_ignores_session_status(harness):
    db_file = harness["db_file"]
    msg_id = await insert_message(
        db_file,
        platform_message_id="om_status_ignored",
        content="已有飞书话题的历史会话",
    )
    await process_message(msg_id)
    msg = await fetch_one(db_file, "SELECT * FROM messages WHERE id = ?", (msg_id,))
    session_id = int(msg["session_id"])
    repo = harness["deps"].repository

    await repo.update_session_patch(
        session_id,
        {"thread_id": "omt_status_ignored", "status": "archived"},
        now="2026-04-30T10:01:00",
    )

    session = await repo.find_open_session_by_thread("omt_status_ignored")

    assert session is not None
    assert session["id"] == session_id
    assert session["status"] == "archived"


@pytest.mark.asyncio
async def test_direct_project_context_updates_session_and_audit(harness, monkeypatch):
    db_file = harness["db_file"]

    monkeypatch.setattr(
        "core.app.message_processor.prepare_direct_project_context",
        lambda ctx, workspace: {
            "running_project": "allspark",
            "project_path": r"D:\standard\riot\allspark",
            "source_path": r"D:\standard\riot\allspark",
            "worktree_path": str(Path(workspace.artifact_roots["worktrees_dir"]) / "allspark" / "current"),
            "execution_path": str(Path(workspace.artifact_roots["worktrees_dir"]) / "allspark" / "current"),
            "session_dir": workspace.artifact_roots["session_dir"],
            "current_branch": "release/3.46.x",
            "current_version": "3.46.16",
        },
    )
    msg_id = await insert_message(db_file, content="allspark 怎么配置")

    await process_message(msg_id)

    msg = await fetch_one(db_file, "SELECT * FROM messages WHERE id = ?", (msg_id,))
    session = await fetch_one(db_file, "SELECT * FROM sessions WHERE id = ?", (msg["session_id"],))
    assert session["project"] == "allspark"
    assert Path(session["analysis_workspace"]).name == f"session-{msg['session_id']}"

    audit = await fetch_one(
        db_file,
        "SELECT detail FROM audit_logs WHERE event_type = ?",
        ("project_workspace_prepared",),
    )
    detail = json.loads(audit["detail"])
    assert detail["running_project"] == "allspark"
    assert detail["project_path"] == r"D:\standard\riot\allspark"


@pytest.mark.asyncio
async def test_first_direct_project_name_uses_fast_entry_path(harness, monkeypatch, tmp_path):
    db_file = harness["db_file"]
    project_path = tmp_path / "repo" / "allspark"
    project_path.mkdir(parents=True)

    monkeypatch.setattr(
        "core.app.message_processor.prepare_direct_project_context",
        lambda ctx, workspace: {
            "running_project": "allspark",
            "project_path": str(project_path),
            "source_path": str(project_path),
            "worktree_path": str(Path(workspace.artifact_roots["worktrees_dir"]) / "allspark" / "current"),
            "execution_path": str(Path(workspace.artifact_roots["worktrees_dir"]) / "allspark" / "current"),
            "session_dir": workspace.artifact_roots["session_dir"],
            "current_branch": "release/3.46.x",
            "current_version": "3.46.16",
            "execution_version": "3.46.16",
        },
    )
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
    msg_id = await insert_message(db_file, content="allspark")

    await process_message(msg_id)

    assert harness["agent_port"].calls == []
    assert len(harness["channel"].calls) == 1
    reply = harness["channel"].calls[0]["reply"]
    assert reply.type == "feishu_card"
    assert reply.intent == "project_context_ready"
    assert "已进入 allspark" in reply.content

    msg = await fetch_one(db_file, "SELECT * FROM messages WHERE id = ?", (msg_id,))
    session = await fetch_one(db_file, "SELECT * FROM sessions WHERE id = ?", (msg["session_id"],))
    assert session["project"] == "allspark"
    assert Path(session["analysis_workspace"]).name == f"session-{msg['session_id']}"

    audits = await fetch_all(db_file, "SELECT event_type FROM audit_logs ORDER BY id")
    event_types = [item["event_type"] for item in audits]
    assert "project_entry_fast_path" in event_types
    assert "project_entry_acknowledged" in event_types
    assert "agent_run_started" not in event_types


@pytest.mark.asyncio
async def test_session_164_frontend_switch_prepares_worktree_before_analysis(harness, monkeypatch, tmp_path):
    db_file = harness["db_file"]
    repos_dir = tmp_path / "repos"
    allspark_repo = _init_repo(repos_dir / "allspark", initial_branch="master", tag="3.52.0")
    frontend_repo = _init_repo(repos_dir / "riot-frontend-v3", initial_branch="main", tag="v3.51.0")
    projects = [
        ProjectConfig(
            name="allspark",
            path=allspark_repo,
            description="Riot 调度系统 3.0（allspark）。别名：riot3、RIOT3、riot3调度系统。",
        ),
        ProjectConfig(
            name="riot-frontend-v3",
            path=frontend_repo,
            description="RIOT3 前端项目（riot-frontend-v3）。别名：riot3前端、riot3 前端、RIOT3前端、RIOT3 前端、riot3 frontend、riot3-frontend、allspark frontend。",
        ),
    ]
    monkeypatch.setattr("core.app.project_context.get_projects", lambda: projects)
    monkeypatch.setattr(projects_mod, "get_projects", lambda: projects)
    monkeypatch.setattr(
        projects_mod,
        "get_project",
        lambda name: next((project for project in projects if project.name == name), None),
    )
    projects_mod._git_ref_inventory.cache_clear()

    allspark_id = await insert_message(db_file, platform_message_id="om_session164_allspark", content="allspark")
    await process_message(allspark_id)
    allspark_msg = await fetch_one(db_file, "SELECT * FROM messages WHERE id = ?", (allspark_id,))
    session_id = int(allspark_msg["session_id"])
    session_dir = harness["workspace_root"] / f"session-{session_id}"
    project_workspace = json.loads((session_dir / "project_workspace.json").read_text(encoding="utf-8"))
    assert project_workspace["active_project"] == "allspark"
    assert project_workspace["projects"]["allspark"]["worktree_path"].endswith(
        f"worktrees{os.sep}allspark{os.sep}entry-{allspark_id}-master"
    )
    assert harness["agent_port"].calls == []

    frontend_id = await insert_message(
        db_file,
        platform_message_id="om_session164_frontend",
        content="RIOT3 前端",
        thread_id="omt_reply_001",
    )
    await process_message(frontend_id)
    project_workspace = json.loads((session_dir / "project_workspace.json").read_text(encoding="utf-8"))
    assert project_workspace["active_project"] == "riot-frontend-v3"
    assert set(project_workspace["projects"]) == {"allspark", "riot-frontend-v3"}
    frontend_entry = project_workspace["projects"]["riot-frontend-v3"]
    assert frontend_entry["worktree_path"].endswith(
        f"worktrees{os.sep}riot-frontend-v3{os.sep}entry-{frontend_id}-main"
    )
    assert Path(frontend_entry["worktree_path"]).is_dir()
    assert harness["agent_port"].calls == []

    harness["agent_port"].results.append(_reply_result("frontend analysis"))
    followup_id = await insert_message(
        db_file,
        platform_message_id="om_session164_frontend_followup",
        content="如果后端调整这些信息，前端需要怎么修改",
        thread_id="omt_reply_001",
    )
    await process_message(followup_id)

    assert len(harness["agent_port"].calls) == 1
    agent_workspace = harness["agent_port"].calls[0].workspace_path
    agent_project_workspace = read_workspace_json(agent_workspace, "input/project_workspace.json")
    assert agent_project_workspace["active_project"] == "riot-frontend-v3"
    assert agent_project_workspace["projects"]["riot-frontend-v3"]["worktree_path"] == frontend_entry["worktree_path"]
    assert agent_project_workspace["projects"]["allspark"]["worktree_path"] == project_workspace["projects"]["allspark"]["worktree_path"]

    audits = await fetch_all(db_file, "SELECT event_type, target_id FROM audit_logs ORDER BY id")
    fast_path_targets = [item["target_id"] for item in audits if item["event_type"] == "project_entry_fast_path"]
    assert str(allspark_id) in fast_path_targets
    assert str(frontend_id) in fast_path_targets
    assert str(followup_id) not in fast_path_targets


@pytest.mark.asyncio
async def test_ones_reference_skips_direct_project_context(harness, monkeypatch):
    db_file = harness["db_file"]
    direct_called = False

    async def fake_ones_prefetch(ctx, workspace, *, runtime):
        return SimpleNamespace(
            to_audit_detail=lambda: {
                "reference": "#150552",
                "fetched": True,
                "summary_snapshot_json": str(workspace.path / ".ones" / "summary_snapshot.json"),
            }
        )

    def fake_direct_project_context(ctx, workspace):
        nonlocal direct_called
        direct_called = True
        return {"running_project": "allspark"}

    monkeypatch.setattr("core.app.message_processor.prepare_ones_intake", fake_ones_prefetch)
    monkeypatch.setattr("core.app.message_processor.prepare_direct_project_context", fake_direct_project_context)
    msg_id = await insert_message(
        db_file,
        content=(
            "#150552 【3.52.0】【订单】：订单取消成功，但是订单未与小车解绑\n"
            "https://ones.standard-robots.com:10120/project/#/team/UNrQ5Ny5/task/NbJXtiyGP7R4vYnF"
        ),
    )

    await process_message(msg_id)

    assert direct_called is False
    audits = await fetch_all(db_file, "SELECT event_type FROM audit_logs ORDER BY id")
    event_types = [item["event_type"] for item in audits]
    assert "ones_intake_prepared" in event_types
    assert "project_workspace_prepared" not in event_types


@pytest.mark.asyncio
async def test_feishu_thread_reuses_triaged_session_for_followup_attachment(harness, tmp_path):
    db_file = harness["db_file"]
    first_id = await insert_message(
        db_file,
        platform_message_id="om_triaged_root",
        content="初始分析已完成，等待补日志",
    )
    await process_message(first_id)
    first = await fetch_one(db_file, "SELECT * FROM messages WHERE id = ?", (first_id,))
    assert first["session_id"] is not None

    session_id = int(first["session_id"])
    await harness["deps"].repository.update_session_patch(
        session_id,
        {"thread_id": "omt_triaged_thread", "status": "triaged"},
        now="2026-04-30T10:01:00",
    )
    harness["channel"].thread_id = "omt_triaged_thread"

    log_file = tmp_path / "bootstrap.log.gz"
    log_file.write_bytes(b"\x1f\x8b fake")
    followup_id = await insert_message(
        db_file,
        platform_message_id="om_triaged_file",
        message_type="file",
        content="[文件: bootstrap.log.gz]",
        thread_id="omt_triaged_thread",
        media_info={
            "type": "file",
            "local_path": str(log_file),
            "file_name": "bootstrap.log.gz",
            "mime_type": "application/gzip",
        },
    )

    await process_message(followup_id)

    followup = await fetch_one(db_file, "SELECT * FROM messages WHERE id = ?", (followup_id,))
    session = await fetch_one(db_file, "SELECT * FROM sessions WHERE id = ?", (session_id,))
    assert followup["pipeline_status"] == "completed"
    assert followup["session_id"] == session_id
    assert session["thread_id"] == "omt_triaged_thread"
    assert session["status"] == "triaged"

    workspace_audits = await fetch_all(
        db_file,
        "SELECT detail FROM audit_logs WHERE event_type = 'workspace_prepared' ORDER BY id",
    )
    followup_workspace = Path(json.loads(workspace_audits[-1]["detail"])["workspace_path"])
    assert followup_workspace.parent.name == f"session-{session_id}"


@pytest.mark.asyncio
async def test_new_session_uses_runtime_from_app_settings(harness):
    db_file = harness["db_file"]
    async with aiosqlite.connect(db_file) as db:
        await db.execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            ("current_agent_runtime", "codex", "2026-04-30T10:00:00"),
        )
        await db.commit()

    msg_id = await insert_message(db_file, platform_message_id="om_runtime", content="用 codex")
    await process_message(msg_id)

    assert harness["agent_port"].calls[0].session["agent_runtime"] == "codex"


@pytest.mark.asyncio
async def test_no_reply_marks_completed_without_channel_delivery(harness):
    db_file = harness["db_file"]
    harness["agent_port"].results[:] = [{"action": "no_reply", "skill_trace": []}]
    msg_id = await insert_message(db_file, content="系统通知")

    await process_message(msg_id)

    msg = await fetch_one(db_file, "SELECT pipeline_status FROM messages WHERE id = ?", (msg_id,))
    assert msg["pipeline_status"] == "completed"
    assert harness["channel"].calls == []


@pytest.mark.asyncio
async def test_failed_result_marks_message_failed(harness):
    db_file = harness["db_file"]
    harness["agent_port"].results[:] = [{"action": "failed", "error_message": "bad input"}]
    msg_id = await insert_message(db_file, content="触发失败")

    await process_message(msg_id)

    msg = await fetch_one(db_file, "SELECT pipeline_status, pipeline_error FROM messages WHERE id = ?", (msg_id,))
    assert msg["pipeline_status"] == "failed"
    assert msg["pipeline_error"] == "bad input"
    assert harness["channel"].calls == []


@pytest.mark.asyncio
async def test_malformed_contract_is_repaired_by_model_before_delivery(harness):
    db_file = harness["db_file"]
    harness["agent_port"].results[:] = [
        AgentResponse(
            text=(
                '{"action":"reply","reply":{"channel":"feishu","type":"feishu_card",'
                '"content":"确定性兜底摘要","payload":'
            ),
            runtime="fake",
            raw={},
        ),
        _reply_result("模型修复后的回复"),
    ]
    msg_id = await insert_message(db_file, content="触发坏 contract")

    await process_message(msg_id)

    assert [call.mode for call in harness["agent_port"].calls] == ["process", "repair_contract"]
    assert len(harness["channel"].calls) == 1
    assert harness["channel"].calls[0]["reply"].content == "模型修复后的回复"

    audits = await fetch_all(db_file, "SELECT event_type FROM audit_logs ORDER BY id")
    assert "agent_contract_repair_model_completed" in [item["event_type"] for item in audits]


@pytest.mark.asyncio
async def test_invalid_card_reply_is_repaired_and_revalidated_before_delivery(harness):
    db_file = harness["db_file"]
    harness["agent_port"].results[:] = [
        {
            "action": "reply",
            "reply": {
                "channel": "feishu",
                "type": "feishu_card",
                "content": "坏卡片兜底",
                "payload": {"structured_summary": {"title": "not a card"}},
            },
            "session_patch": {},
            "workspace_patch": {},
            "skill_trace": [{"skill": "test-skill", "reason": "invalid card fixture"}],
            "audit": [],
        },
        _reply_result("模型修复后的卡片兜底"),
    ]
    msg_id = await insert_message(db_file, content="触发坏卡片")

    await process_message(msg_id)

    assert [call.mode for call in harness["agent_port"].calls] == ["process", "repair_reply"]
    assert len(harness["channel"].calls) == 1
    delivered = harness["channel"].calls[0]["reply"]
    assert delivered.type == "markdown"
    assert delivered.content == "模型修复后的卡片兜底"

    audits = await fetch_all(db_file, "SELECT event_type FROM audit_logs ORDER BY id")
    event_types = [item["event_type"] for item in audits]
    assert "reply_validation_failed" in event_types
    assert "reply_repair_started" in event_types
    assert "reply_repair_completed" in event_types


@pytest.mark.asyncio
async def test_agent_timeout_recovers_from_reply_contract_artifact(harness):
    db_file = harness["db_file"]
    agent_port = TimeoutAfterArtifactAgentPort(_reply_result("artifact reply"))
    harness["deps"].agents.agent_port = agent_port
    msg_id = await insert_message(db_file, content="ONES 分析")

    await process_message(msg_id)

    msg = await fetch_one(db_file, "SELECT pipeline_status, pipeline_error FROM messages WHERE id = ?", (msg_id,))
    assert msg == {"pipeline_status": "completed", "pipeline_error": ""}
    assert len(harness["channel"].calls) == 1
    assert harness["channel"].calls[0]["reply"].content == "artifact reply"

    audits = await fetch_all(db_file, "SELECT event_type FROM audit_logs ORDER BY id")
    event_types = [item["event_type"] for item in audits]
    assert "agent_run_recovered_from_artifact" in event_types
    assert "agent_run_failed" not in event_types
    assert "message_failed" not in event_types

    run = await fetch_one(db_file, "SELECT status, error_message FROM agent_runs WHERE message_id = ?", (msg_id,))
    assert run["status"] == "success"
    assert "recovered after agent error" in run["error_message"]


@pytest.mark.asyncio
async def test_agent_timeout_ignores_stale_reply_contract_artifact(harness):
    db_file = harness["db_file"]
    agent_port = StaleArtifactTimeoutAgentPort(_reply_result("stale artifact"))
    harness["deps"].agents.agent_port = agent_port
    msg_id = await insert_message(db_file, content="新问题")

    await process_message(msg_id)

    msg = await fetch_one(db_file, "SELECT pipeline_status, pipeline_error FROM messages WHERE id = ?", (msg_id,))
    assert msg["pipeline_status"] == "failed"
    assert msg["pipeline_error"] == "codex exec timed out (session=new)"
    assert harness["channel"].calls == []

    audits = await fetch_all(db_file, "SELECT event_type FROM audit_logs ORDER BY id")
    event_types = [item["event_type"] for item in audits]
    assert "agent_run_recovered_from_artifact" not in event_types


@pytest.mark.asyncio
async def test_media_is_staged_into_workspace_manifest(harness, tmp_path):
    db_file = harness["db_file"]
    image = tmp_path / "capture.png"
    image.write_bytes(b"\x89PNG fake")
    msg_id = await insert_message(
        db_file,
        platform_message_id="om_image",
        message_type="image",
        content="看下截图",
        media_info={"type": "image", "local_path": str(image), "mime_type": "image/png"},
    )

    await process_message(msg_id)

    workspace = harness["agent_port"].calls[0].workspace_path
    manifest = read_workspace_json(workspace, "input/media_manifest.json")
    assert manifest["items"][0]["kind"] == "image"
    roots = read_workspace_json(workspace, "input/artifact_roots.json")
    staged = Path(manifest["items"][0]["local_path"])
    assert staged.exists()
    assert staged.resolve().is_relative_to(Path(roots["uploads_dir"]).resolve())
    assert staged.parent == Path(roots["uploads_dir"])
    assert staged.name.startswith(f"message-{msg_id}_")
    assert staged.read_bytes() == b"\x89PNG fake"


@pytest.mark.asyncio
async def test_attachment_only_message_is_acknowledged_without_agent(harness, tmp_path):
    db_file = harness["db_file"]
    log_file = tmp_path / "bootstrap.log.gz"
    log_file.write_bytes(b"\x1f\x8b fake")
    msg_id = await insert_message(
        db_file,
        platform_message_id="om_file",
        message_type="file",
        content="[文件: bootstrap.log.gz]",
        media_info={
            "type": "file",
            "local_path": str(log_file),
            "file_name": "bootstrap.log.gz",
            "mime_type": "application/gzip",
        },
    )

    await process_message(msg_id)

    msg = await fetch_one(db_file, "SELECT pipeline_status FROM messages WHERE id = ?", (msg_id,))
    assert msg["pipeline_status"] == "completed"
    assert harness["agent_port"].calls == []
    assert len(harness["channel"].calls) == 1
    reply = harness["channel"].calls[0]["reply"]
    assert reply.type == "markdown"
    assert "已收到附件" in reply.content
    assert "uploads" in reply.content

    workspace_audit = await fetch_one(
        db_file,
        "SELECT detail FROM audit_logs WHERE event_type = ?",
        ("workspace_prepared",),
    )
    workspace = Path(json.loads(workspace_audit["detail"])["workspace_path"])
    manifest = read_workspace_json(workspace, "input/media_manifest.json")
    assert manifest["items"][0]["kind"] == "file"
    assert Path(manifest["items"][0]["local_path"]).exists()

    audits = await fetch_all(db_file, "SELECT event_type FROM audit_logs ORDER BY id")
    assert [item["event_type"] for item in audits] == [
        "message_processing_started",
        "workspace_prepared",
        "attachment_only_message_acknowledged",
        "reply_delivery_started",
        "message_completed",
    ]


@pytest.mark.asyncio
async def test_reprocess_resets_and_runs_again(harness):
    db_file = harness["db_file"]
    msg_id = await insert_message(db_file, content="重试")
    await process_message(msg_id)

    harness["agent_port"].results.append(_reply_result("second pass"))
    await reprocess_message(msg_id)

    msg = await fetch_one(db_file, "SELECT pipeline_status, pipeline_error FROM messages WHERE id = ?", (msg_id,))
    assert msg == {"pipeline_status": "completed", "pipeline_error": ""}
    assert len(harness["agent_port"].calls) == 2


def _reply_result(content: str) -> dict:
    return {
        "action": "reply",
        "reply": {
            "channel": "feishu",
            "type": "markdown",
            "intent": "needs_input" if "缺" in content else None,
            "content": content,
            "payload": None,
        },
        "session_patch": {},
        "workspace_patch": {},
        "skill_trace": [{"skill": "test-skill", "reason": "fixture"}],
        "audit": [],
    }


def _init_repo(path: Path, *, initial_branch: str, tag: str = "") -> Path:
    path.mkdir(parents=True)
    _git(path, "init", f"--initial-branch={initial_branch}")
    _git(path, "config", "user.name", "Test User")
    _git(path, "config", "user.email", "test@example.com")
    (path / "README.md").write_text(path.name + "\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "init")
    if tag:
        _git(path, "tag", tag)
    return path


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


class TimeoutAfterArtifactAgentPort:
    def __init__(self, artifact_result: dict) -> None:
        self.artifact_result = artifact_result
        self.calls: list[AgentRequest] = []

    async def run(self, request: AgentRequest) -> AgentResponse:
        self.calls.append(request)
        artifact_path = request.workspace_path / "output" / "reply_contract_150552.json"
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(json.dumps(self.artifact_result, ensure_ascii=False), encoding="utf-8")
        raise RuntimeError("codex exec timed out (session=new)")


class StaleArtifactTimeoutAgentPort(TimeoutAfterArtifactAgentPort):
    async def run(self, request: AgentRequest) -> AgentResponse:
        self.calls.append(request)
        artifact_path = request.workspace_path / "output" / "reply_contract_150552.json"
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(json.dumps(self.artifact_result, ensure_ascii=False), encoding="utf-8")
        os.utime(artifact_path, (1, 1))
        raise RuntimeError("codex exec timed out (session=new)")
