from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import aiosqlite
import pytest


_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT DEFAULT '',
    agent_runtime TEXT DEFAULT 'claude',
    agent_session_id TEXT DEFAULT '',
    analysis_mode INTEGER DEFAULT 0,
    analysis_workspace TEXT DEFAULT '',
    updated_at TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS agent_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name TEXT,
    runtime_type TEXT,
    session_id INTEGER,
    message_id INTEGER,
    status TEXT,
    input_path TEXT DEFAULT '',
    output_path TEXT DEFAULT '',
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0,
    error_message TEXT DEFAULT '',
    started_at TEXT,
    ended_at TEXT
);
"""


@pytest.mark.asyncio
async def test_dispatch_to_project_uses_riot_log_triage_for_analysis_session(tmp_path, monkeypatch):
    from core.orchestrator import agent_client as agent_client_mod
    import core.projects as projects_mod

    monkeypatch.setattr(agent_client_mod, "PROJECT_ROOT", tmp_path)
    db_dir = tmp_path / "data" / "db"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "app.sqlite"

    analysis_dir = tmp_path / ".triage" / "case-1"
    analysis_dir.mkdir(parents=True, exist_ok=True)
    state_path = analysis_dir / "00-state.json"
    state_path.write_text(json.dumps({
        "project": "allspark",
        "mode": "structured",
        "phase": "keywords_ready",
        "current_question": "问题时间点车辆处于什么状态、卡在哪个流程门禁",
        "primary_question": "为什么 AG0019 没继续出梯",
        "analysis_workspace": str(analysis_dir),
        "agent_context": {
            "session_id": "triage-session-old",
            "runtime": "claude",
            "skill": "riot-log-triage",
        },
        "search_artifacts": {
            "keyword_package_round1": str(analysis_dir / "keyword_package.round1.json"),
        },
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    async with aiosqlite.connect(db_path) as db:
        await db.executescript(_SCHEMA)
        await db.execute(
            "INSERT INTO sessions (id, project, agent_runtime, agent_session_id, analysis_mode, analysis_workspace, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (88, "allspark", "claude", "sdk-project-session", 1, str(analysis_dir), datetime.now().isoformat()),
        )
        await db.commit()

    monkeypatch.setattr(
        projects_mod,
        "get_project",
        lambda name: SimpleNamespace(name=name, path=tmp_path / "repo" / name, description="demo project"),
    )
    monkeypatch.setattr(
        projects_mod,
        "prepare_project_runtime_context",
        lambda project_name, ones_result=None: SimpleNamespace(
            execution_path=tmp_path / "repo" / project_name,
            to_payload=lambda: {"running_project": project_name, "execution_path": str(tmp_path / "repo" / project_name)},
        ),
    )
    monkeypatch.setattr(
        projects_mod,
        "merge_skills",
        lambda *args, **kwargs: {
            "ones": SimpleNamespace(description="ones", prompt="ones"),
            "riot-log-triage": SimpleNamespace(description="triage", prompt="triage"),
            "other": SimpleNamespace(description="other", prompt="other"),
        },
    )

    captured: dict[str, object] = {}

    async def fake_run_for_project(**kwargs):
        captured.update(kwargs)
        return {"text": "triage result", "session_id": "triage-session-new", "cost_usd": 0.0}

    monkeypatch.setattr(agent_client_mod.agent_client, "run_for_project", fake_run_for_project)

    result = await agent_client_mod.dispatch_to_project.handler({
        "project_name": "allspark",
        "task": "请继续排查这单日志问题",
        "context": "请继续沿当前 triage 状态检查 CrossMapManager 日志",
        "skill": "riot-log-triage",
        "session_id": "sdk-project-session",
        "db_session_id": 88,
    })

    assert result["session_id"] == "triage-session-new"
    assert captured["skill"] == "riot-log-triage"
    assert captured["session_id"] is None
    assert set(captured["project_agents"]) == {"riot-log-triage", "other"}

    updated_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert updated_state["agent_context"]["session_id"] == "triage-session-old"
    assert updated_state["agent_context"]["skill"] == "riot-log-triage"
    assert result["agent_session_scope"] == "record_only"

    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute("SELECT agent_session_id FROM sessions WHERE id = 88")
        row = await cursor.fetchone()
    assert row[0] == "sdk-project-session"


@pytest.mark.asyncio
async def test_dispatch_to_project_correction_turn_resets_saved_triage_session(tmp_path, monkeypatch):
    from core.orchestrator import agent_client as agent_client_mod
    import core.projects as projects_mod

    monkeypatch.setattr(agent_client_mod, "PROJECT_ROOT", tmp_path)
    db_dir = tmp_path / "data" / "db"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "app.sqlite"

    analysis_dir = tmp_path / ".triage" / "case-1"
    analysis_dir.mkdir(parents=True, exist_ok=True)
    (analysis_dir / "00-state.json").write_text(json.dumps({
        "project": "allspark",
        "mode": "structured",
        "phase": "keywords_ready",
        "agent_context": {
            "session_id": "triage-session-old",
            "runtime": "claude",
            "skill": "riot-log-triage",
        },
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    async with aiosqlite.connect(db_path) as db:
        await db.executescript(_SCHEMA)
        await db.execute(
            "INSERT INTO sessions (id, project, agent_runtime, agent_session_id, analysis_mode, analysis_workspace, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (88, "allspark", "claude", "sdk-project-session", 1, str(analysis_dir), datetime.now().isoformat()),
        )
        await db.commit()

    monkeypatch.setattr(
        projects_mod,
        "get_project",
        lambda name: SimpleNamespace(name=name, path=tmp_path / "repo" / name, description="demo project"),
    )
    monkeypatch.setattr(
        projects_mod,
        "prepare_project_runtime_context",
        lambda project_name, ones_result=None: SimpleNamespace(
            execution_path=tmp_path / "repo" / project_name,
            to_payload=lambda: {"running_project": project_name, "execution_path": str(tmp_path / "repo" / project_name)},
        ),
    )
    monkeypatch.setattr(
        projects_mod,
        "merge_skills",
        lambda *args, **kwargs: {
            "ones": SimpleNamespace(description="ones", prompt="ones"),
            "riot-log-triage": SimpleNamespace(description="triage", prompt="triage"),
        },
    )

    captured: dict[str, object] = {}

    async def fake_run_for_project(**kwargs):
        captured.update(kwargs)
        return {"text": "triage result", "session_id": "triage-session-new", "cost_usd": 0.0}

    monkeypatch.setattr(agent_client_mod.agent_client, "run_for_project", fake_run_for_project)

    await agent_client_mod.dispatch_to_project.handler({
        "project_name": "allspark",
        "task": "请继续排查这单日志问题",
        "context": "CrossMapManager 日志打印出来，http 是干扰项",
        "skill": "riot-log-triage",
        "session_id": "sdk-project-session",
        "db_session_id": 88,
    })

    assert captured["skill"] == "riot-log-triage"
    assert captured["session_id"] is None


@pytest.mark.asyncio
async def test_dispatch_to_project_does_not_auto_select_skill_from_analysis_state(tmp_path, monkeypatch):
    from core.orchestrator import agent_client as agent_client_mod
    import core.projects as projects_mod

    monkeypatch.setattr(agent_client_mod, "PROJECT_ROOT", tmp_path)
    db_dir = tmp_path / "data" / "db"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "app.sqlite"

    analysis_dir = tmp_path / ".triage" / "case-1"
    analysis_dir.mkdir(parents=True, exist_ok=True)
    (analysis_dir / "00-state.json").write_text(json.dumps({
        "project": "allspark",
        "agent_context": {
            "session_id": "triage-session-old",
            "runtime": "claude",
            "skill": "riot-log-triage",
        },
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    async with aiosqlite.connect(db_path) as db:
        await db.executescript(_SCHEMA)
        await db.execute(
            "INSERT INTO sessions (id, project, agent_runtime, agent_session_id, analysis_mode, analysis_workspace, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (88, "allspark", "claude", "sdk-project-session", 1, str(analysis_dir), datetime.now().isoformat()),
        )
        await db.commit()

    monkeypatch.setattr(
        projects_mod,
        "get_project",
        lambda name: SimpleNamespace(name=name, path=tmp_path / "repo" / name, description="demo project"),
    )
    monkeypatch.setattr(
        projects_mod,
        "prepare_project_runtime_context",
        lambda project_name, ones_result=None: SimpleNamespace(
            execution_path=tmp_path / "repo" / project_name,
            to_payload=lambda: {"running_project": project_name, "execution_path": str(tmp_path / "repo" / project_name)},
        ),
    )
    monkeypatch.setattr(
        projects_mod,
        "merge_skills",
        lambda *args, **kwargs: {
            "riot-log-triage": SimpleNamespace(description="triage", prompt="triage"),
        },
    )

    captured: dict[str, object] = {}

    async def fake_run_for_project(**kwargs):
        captured.update(kwargs)
        return {"text": "project result", "session_id": "plain-session-new", "cost_usd": 0.0}

    monkeypatch.setattr(agent_client_mod.agent_client, "run_for_project", fake_run_for_project)

    result = await agent_client_mod.dispatch_to_project.handler({
        "project_name": "allspark",
        "task": "普通项目问题",
        "context": "由主编排决定不使用 triage skill",
        "session_id": "sdk-project-session",
        "db_session_id": 88,
    })

    assert result["session_id"] == "plain-session-new"
    assert captured["skill"] is None
    assert captured["session_id"] is None

    updated_state = json.loads((analysis_dir / "00-state.json").read_text(encoding="utf-8"))
    assert updated_state["agent_context"]["session_id"] == "triage-session-old"


@pytest.mark.asyncio
async def test_dispatch_to_project_auto_selects_riot_log_triage_for_vehicle_task(tmp_path, monkeypatch):
    from core.artifacts.session_init import initialize_session_workspace
    from core.orchestrator import agent_client as agent_client_mod
    from core.app import project_workspace as project_workspace_mod
    import core.projects as projects_mod

    monkeypatch.setattr(agent_client_mod, "PROJECT_ROOT", tmp_path)
    db_dir = tmp_path / "data" / "db"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "app.sqlite"

    session_dir = tmp_path / "data" / "sessions" / "session-88"
    initialize_session_workspace(session_dir / "workspace", 88)
    (session_dir / "uploads" / "message-10_bootstrap.log.2026-03-25-05.2.gz").write_bytes(b"log")
    (session_dir / "uploads" / "message-11_media-1.png").write_bytes(b"\x89PNG\r\n\x1a\nimage")

    async with aiosqlite.connect(db_path) as db:
        await db.executescript(_SCHEMA)
        await db.execute(
            "INSERT INTO sessions (id, project, agent_runtime, agent_session_id, analysis_mode, analysis_workspace, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (88, "allspark", "codex", "orchestrator-session", 0, str(session_dir), datetime.now().isoformat()),
        )
        await db.commit()

    project = SimpleNamespace(name="allspark", path=tmp_path / "repo" / "allspark", description="backend")
    project.path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(projects_mod, "get_project", lambda name: project if name == "allspark" else None)
    monkeypatch.setattr(
        projects_mod,
        "merge_skills",
        lambda *args, **kwargs: {
            "ones": SimpleNamespace(description="ones", prompt="ones prompt"),
            "riot-log-triage": SimpleNamespace(description="triage", prompt="triage prompt"),
            "other": SimpleNamespace(description="other", prompt="other prompt"),
        },
    )

    def fake_prepare_project_runtime_context(project_name, ones_result=None, worktree_root=None, worktree_token=""):
        execution_path = Path(worktree_root) / project_name / "stable-main"
        execution_path.mkdir(parents=True, exist_ok=True)
        return SimpleNamespace(
            execution_path=execution_path,
            to_payload=lambda: {
                "running_project": project_name,
                "project_path": str(project.path),
                "execution_path": str(execution_path),
                "worktree_path": str(execution_path),
                "checkout_ref": "main",
                "execution_commit_sha": "sha",
                "execution_version": "test-version",
            },
        )

    monkeypatch.setattr(project_workspace_mod, "prepare_project_runtime_context", fake_prepare_project_runtime_context)

    captured: dict[str, object] = {}

    async def fake_run_for_project(**kwargs):
        captured.update(kwargs)
        return {"text": "triage result", "session_id": "triage-project-session", "cost_usd": 0.0}

    monkeypatch.setattr(agent_client_mod.agent_client, "get_active_runtime", lambda: "codex")
    monkeypatch.setattr(agent_client_mod.agent_client, "run_for_project", fake_run_for_project)

    result = await agent_client_mod.dispatch_to_project.handler({
        "project_name": "allspark",
        "task": "这个B-3T-1 SeerAGV 会出现，车辆直接自由导航到5001，下发的没有经过5036. 检查一下是为什么",
        "context": "前序已上传 bootstrap.log.2026-03-25-05.2.gz 和两张截图",
        "db_session_id": 88,
        "message_id": 1110,
    })

    assert result["session_id"] == "triage-project-session"
    assert result["triage_dir"]
    assert captured["skill"] == "riot-log-triage"
    assert set(captured["project_agents"]) == {"riot-log-triage", "other"}
    assert "必须使用 riot-log-triage workflow skill" in captured["prompt"]
    assert "关联 Triage 目录" in captured["prompt"]

    input_payload = json.loads((Path(result["analysis_dir"]) / "input.json").read_text(encoding="utf-8"))
    assert input_payload["skill"] == "riot-log-triage"
    assert input_payload["triage_dir"] == result["triage_dir"]

    dispatch_payload = json.loads((session_dir / ".orchestration" / "message-1110" / "dispatch-001.json").read_text(encoding="utf-8"))
    assert dispatch_payload["skill"] == "riot-log-triage"
    assert dispatch_payload["requested_skill"] == ""
    assert dispatch_payload["auto_selected_skill"] == "riot-log-triage"
    assert dispatch_payload["triage_dir"] == result["triage_dir"]
    assert (Path(result["triage_dir"]) / "00-state.json").is_file()


@pytest.mark.asyncio
async def test_dispatch_to_project_does_not_auto_select_riot_log_triage_for_generic_question(tmp_path, monkeypatch):
    from core.artifacts.session_init import initialize_session_workspace
    from core.orchestrator import agent_client as agent_client_mod
    from core.app import project_workspace as project_workspace_mod
    import core.projects as projects_mod

    monkeypatch.setattr(agent_client_mod, "PROJECT_ROOT", tmp_path)
    db_dir = tmp_path / "data" / "db"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "app.sqlite"

    session_dir = tmp_path / "data" / "sessions" / "session-88"
    initialize_session_workspace(session_dir / "workspace", 88)

    async with aiosqlite.connect(db_path) as db:
        await db.executescript(_SCHEMA)
        await db.execute(
            "INSERT INTO sessions (id, project, agent_runtime, agent_session_id, analysis_mode, analysis_workspace, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (88, "allspark", "codex", "orchestrator-session", 0, str(session_dir), datetime.now().isoformat()),
        )
        await db.commit()

    project = SimpleNamespace(name="allspark", path=tmp_path / "repo" / "allspark", description="backend")
    project.path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(projects_mod, "get_project", lambda name: project if name == "allspark" else None)
    monkeypatch.setattr(
        projects_mod,
        "merge_skills",
        lambda *args, **kwargs: {
            "riot-log-triage": SimpleNamespace(description="triage", prompt="triage prompt"),
            "other": SimpleNamespace(description="other", prompt="other prompt"),
        },
    )

    def fake_prepare_project_runtime_context(project_name, ones_result=None, worktree_root=None, worktree_token=""):
        execution_path = Path(worktree_root) / project_name / "stable-main"
        execution_path.mkdir(parents=True, exist_ok=True)
        return SimpleNamespace(
            execution_path=execution_path,
            to_payload=lambda: {
                "running_project": project_name,
                "project_path": str(project.path),
                "execution_path": str(execution_path),
                "worktree_path": str(execution_path),
                "checkout_ref": "main",
                "execution_commit_sha": "sha",
                "execution_version": "test-version",
            },
        )

    monkeypatch.setattr(project_workspace_mod, "prepare_project_runtime_context", fake_prepare_project_runtime_context)

    captured: dict[str, object] = {}

    async def fake_run_for_project(**kwargs):
        captured.update(kwargs)
        return {"text": "project result", "session_id": "plain-project-session", "cost_usd": 0.0}

    monkeypatch.setattr(agent_client_mod.agent_client, "get_active_runtime", lambda: "codex")
    monkeypatch.setattr(agent_client_mod.agent_client, "run_for_project", fake_run_for_project)

    result = await agent_client_mod.dispatch_to_project.handler({
        "project_name": "allspark",
        "task": "说明 allspark 的 SQLServer 配置入口",
        "context": "普通项目配置咨询，不涉及现场日志或订单车辆执行链路",
        "db_session_id": 88,
        "message_id": 1110,
    })

    assert result["session_id"] == "plain-project-session"
    assert result["triage_dir"] == ""
    assert captured["skill"] is None
    assert "必须使用 riot-log-triage workflow skill" not in captured["prompt"]

    dispatch_payload = json.loads((session_dir / ".orchestration" / "message-1110" / "dispatch-001.json").read_text(encoding="utf-8"))
    assert dispatch_payload["skill"] == ""
    assert dispatch_payload["auto_selected_skill"] == ""


@pytest.mark.asyncio
async def test_dispatch_to_project_honors_negative_triage_context_even_with_uploads(tmp_path, monkeypatch):
    from core.artifacts.session_init import initialize_session_workspace
    from core.orchestrator import agent_client as agent_client_mod
    from core.app import project_workspace as project_workspace_mod
    import core.projects as projects_mod

    monkeypatch.setattr(agent_client_mod, "PROJECT_ROOT", tmp_path)
    db_dir = tmp_path / "data" / "db"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "app.sqlite"

    session_dir = tmp_path / "data" / "sessions" / "session-88"
    initialize_session_workspace(session_dir / "workspace", 88)
    (session_dir / "uploads" / "message-10_bootstrap.log.gz").write_bytes(b"log")

    async with aiosqlite.connect(db_path) as db:
        await db.executescript(_SCHEMA)
        await db.execute(
            "INSERT INTO sessions (id, project, agent_runtime, agent_session_id, analysis_mode, analysis_workspace, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (88, "allspark", "codex", "orchestrator-session", 0, str(session_dir), datetime.now().isoformat()),
        )
        await db.commit()

    project = SimpleNamespace(name="allspark", path=tmp_path / "repo" / "allspark", description="backend")
    project.path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(projects_mod, "get_project", lambda name: project if name == "allspark" else None)
    monkeypatch.setattr(
        projects_mod,
        "merge_skills",
        lambda *args, **kwargs: {"riot-log-triage": SimpleNamespace(description="triage", prompt="triage prompt")},
    )

    def fake_prepare_project_runtime_context(project_name, ones_result=None, worktree_root=None, worktree_token=""):
        execution_path = Path(worktree_root) / project_name / "stable-main"
        execution_path.mkdir(parents=True, exist_ok=True)
        return SimpleNamespace(
            execution_path=execution_path,
            to_payload=lambda: {
                "running_project": project_name,
                "project_path": str(project.path),
                "execution_path": str(execution_path),
                "worktree_path": str(execution_path),
                "checkout_ref": "main",
                "execution_commit_sha": "sha",
                "execution_version": "test-version",
            },
        )

    monkeypatch.setattr(project_workspace_mod, "prepare_project_runtime_context", fake_prepare_project_runtime_context)

    captured: dict[str, object] = {}

    async def fake_run_for_project(**kwargs):
        captured.update(kwargs)
        return {"text": "project result", "session_id": "plain-project-session", "cost_usd": 0.0}

    monkeypatch.setattr(agent_client_mod.agent_client, "get_active_runtime", lambda: "codex")
    monkeypatch.setattr(agent_client_mod.agent_client, "run_for_project", fake_run_for_project)

    result = await agent_client_mod.dispatch_to_project.handler({
        "project_name": "allspark",
        "task": "说明 allspark 的 SQLServer 配置入口",
        "context": "前面有日志上传，但本轮不涉及日志，只是普通配置咨询",
        "db_session_id": 88,
        "message_id": 1110,
    })

    assert result["triage_dir"] == ""
    assert captured["skill"] is None


@pytest.mark.asyncio
async def test_dispatch_to_project_auto_selects_triage_before_project_preflight_timeout(tmp_path, monkeypatch):
    from core.artifacts.session_init import initialize_session_workspace
    from core.orchestrator import agent_client as agent_client_mod
    from core.app import project_workspace as project_workspace_mod
    import core.projects as projects_mod

    monkeypatch.setattr(agent_client_mod, "PROJECT_ROOT", tmp_path)
    db_dir = tmp_path / "data" / "db"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "app.sqlite"
    session_dir = tmp_path / "data" / "sessions" / "session-88"
    initialize_session_workspace(session_dir / "workspace", 88)
    (session_dir / "uploads" / "message-10_bootstrap.log.gz").write_bytes(b"log")
    (tmp_path / ".claude" / "skills" / "riot-log-triage").mkdir(parents=True)
    (tmp_path / ".claude" / "skills" / "riot-log-triage" / "SKILL.md").write_text(
        "---\nname: riot-log-triage\ndescription: triage\n---\n\n# Triage\n",
        encoding="utf-8",
    )

    async with aiosqlite.connect(db_path) as db:
        await db.executescript(_SCHEMA)
        await db.execute(
            "INSERT INTO sessions (id, project, agent_runtime, agent_session_id, analysis_mode, analysis_workspace, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (88, "allspark", "codex", "orchestrator-session", 0, str(session_dir), datetime.now().isoformat()),
        )
        await db.commit()

    project = SimpleNamespace(name="allspark", path=tmp_path / "repo" / "allspark", description="backend")
    project.path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(projects_mod, "get_project", lambda name: project if name == "allspark" else None)
    monkeypatch.setattr(projects_mod, "merge_skills", lambda *args, **kwargs: {
        "riot-log-triage": SimpleNamespace(description="triage", prompt="triage prompt"),
    })

    async def instant_timeout(awaitable, timeout):
        awaitable.close()
        raise TimeoutError

    def never_called(*args, **kwargs):
        raise AssertionError("project preparation should time out before agent run")

    monkeypatch.setattr(project_workspace_mod, "prepare_project_runtime_context", never_called)
    monkeypatch.setattr(agent_client_mod.agent_client, "get_active_runtime", lambda: "codex")
    monkeypatch.setattr(agent_client_mod.agent_client, "run_for_project", never_called)
    monkeypatch.setattr(agent_client_mod.dispatch_to_project.handler.__globals__["asyncio"], "wait_for", instant_timeout)

    result = await agent_client_mod.dispatch_to_project.handler({
        "project_name": "allspark",
        "task": "这个B-3T-1 SeerAGV 会出现，车辆直接自由导航到5001，下发的没有经过5036. 检查一下是为什么",
        "context": "前序已上传 bootstrap.log.gz 和截图",
        "db_session_id": 88,
        "message_id": 1110,
    })

    assert "preflight timed out" in result["error"]
    assert result["triage_dir"]
    analysis_dir = Path(result["analysis_dir"])
    input_payload = json.loads((analysis_dir / "input.json").read_text(encoding="utf-8"))
    assert input_payload["skill"] == "riot-log-triage"
    assert input_payload["triage_dir"] == result["triage_dir"]
    assert (Path(result["triage_dir"]) / "00-state.json").is_file()

    dispatch_payload = json.loads((session_dir / ".orchestration" / "message-1110" / "dispatch-001.json").read_text(encoding="utf-8"))
    assert dispatch_payload["status"] == "preflight_timeout"
    assert dispatch_payload["skill"] == "riot-log-triage"
    assert dispatch_payload["auto_selected_skill"] == "riot-log-triage"


@pytest.mark.asyncio
async def test_dispatch_to_project_timeout_marks_failed_and_writes_artifacts(tmp_path, monkeypatch):
    from core.artifacts.session_init import initialize_session_workspace
    from core.orchestrator import agent_client as agent_client_mod
    from core.app import project_workspace as project_workspace_mod
    import core.projects as projects_mod

    monkeypatch.setattr(agent_client_mod, "PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("PROJECT_DISPATCH_TIMEOUT_SECONDS", "1")
    db_dir = tmp_path / "data" / "db"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "app.sqlite"
    session_dir = tmp_path / "data" / "sessions" / "session-88"
    initialize_session_workspace(session_dir / "workspace", 88)

    async with aiosqlite.connect(db_path) as db:
        await db.executescript(_SCHEMA)
        await db.execute(
            "INSERT INTO sessions (id, project, agent_runtime, agent_session_id, analysis_mode, analysis_workspace, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (88, "allspark", "codex", "orchestrator-session", 0, str(session_dir), datetime.now().isoformat()),
        )
        await db.commit()

    project = SimpleNamespace(name="allspark", path=tmp_path / "repo" / "allspark", description="backend")
    project.path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(projects_mod, "get_project", lambda name: project if name == "allspark" else None)
    monkeypatch.setattr(projects_mod, "merge_skills", lambda *args, **kwargs: {})

    def fake_prepare_project_runtime_context(project_name, ones_result=None, worktree_root=None, worktree_token=""):
        execution_path = Path(worktree_root) / project_name / "stable-main"
        execution_path.mkdir(parents=True, exist_ok=True)
        return SimpleNamespace(
            execution_path=execution_path,
            to_payload=lambda: {
                "running_project": project_name,
                "project_path": str(project.path),
                "execution_path": str(execution_path),
                "worktree_path": str(execution_path),
                "checkout_ref": "main",
                "execution_commit_sha": "sha",
                "execution_version": "test-version",
            },
        )

    monkeypatch.setattr(project_workspace_mod, "prepare_project_runtime_context", fake_prepare_project_runtime_context)

    async def fake_run_for_project(**kwargs):
        import asyncio

        await asyncio.sleep(60)
        return {"text": "too late", "session_id": "project-session"}

    monkeypatch.setattr(agent_client_mod.agent_client, "get_active_runtime", lambda: "codex")
    monkeypatch.setattr(agent_client_mod.agent_client, "run_for_project", fake_run_for_project)

    result = await agent_client_mod.dispatch_to_project.handler({
        "project_name": "allspark",
        "task": "分析项目",
        "db_session_id": 88,
        "message_id": 1001,
    })

    assert "timed out after 1s" in result["error"]
    assert result["agent_run_id"] == 1
    result_payload = json.loads(Path(result["result_path"]).read_text(encoding="utf-8"))
    assert result_payload["failed"] is True
    assert result_payload["status"] == "timeout"
    dispatch_payload = json.loads((session_dir / ".orchestration" / "message-1001" / "dispatch-001.json").read_text(encoding="utf-8"))
    assert dispatch_payload["status"] == "timeout"
    assert "timed out after 1s" in dispatch_payload["error"]

    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT message_id, status, output_path, error_message FROM agent_runs WHERE id = 1"
        )
        row = await cursor.fetchone()
    assert row[0] == 1001
    assert row[1] == "failed"
    assert row[2] == result["result_path"]
    assert "timed out after 1s" in row[3]


@pytest.mark.asyncio
async def test_dispatch_to_project_reuses_ready_workspace_without_preparing_again(tmp_path, monkeypatch):
    from core.artifacts.session_init import initialize_session_workspace
    from core.orchestrator import agent_client as agent_client_mod
    from core.app import project_workspace as project_workspace_mod
    import core.projects as projects_mod

    monkeypatch.setattr(agent_client_mod, "PROJECT_ROOT", tmp_path)
    db_dir = tmp_path / "data" / "db"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "app.sqlite"
    session_dir = tmp_path / "data" / "sessions" / "session-88"
    initialized = initialize_session_workspace(session_dir / "workspace", 88)
    worktree = session_dir / "worktrees" / "allspark" / "entry-1-master"
    worktree.mkdir(parents=True)

    project_workspace = json.loads(initialized.project_workspace_path.read_text(encoding="utf-8"))
    project_workspace.update({
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
            },
        },
    })
    initialized.project_workspace_path.write_text(json.dumps(project_workspace, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (initialized.input_dir / "project_workspace.json").write_text(json.dumps(project_workspace, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    async with aiosqlite.connect(db_path) as db:
        await db.executescript(_SCHEMA)
        await db.execute(
            "INSERT INTO sessions (id, project, agent_runtime, agent_session_id, analysis_mode, analysis_workspace, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (88, "allspark", "codex", "orchestrator-session", 0, str(session_dir), datetime.now().isoformat()),
        )
        await db.commit()

    project = SimpleNamespace(name="allspark", path=tmp_path / "repo" / "allspark", description="backend")
    project.path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(projects_mod, "get_project", lambda name: project if name == "allspark" else None)
    monkeypatch.setattr(projects_mod, "merge_skills", lambda *args, **kwargs: {})

    def fail_prepare(*args, **kwargs):
        raise AssertionError("ready project workspace should be reused")

    monkeypatch.setattr(project_workspace_mod, "prepare_project_from_session_workspace_path", fail_prepare)
    monkeypatch.setattr(project_workspace_mod, "prepare_project_runtime_context", fail_prepare)

    captured: dict[str, object] = {}

    async def fake_run_for_project(**kwargs):
        captured.update(kwargs)
        return {"text": "project result", "session_id": "project-session", "cost_usd": 0.0}

    monkeypatch.setattr(agent_client_mod.agent_client, "get_active_runtime", lambda: "codex")
    monkeypatch.setattr(agent_client_mod.agent_client, "run_for_project", fake_run_for_project)

    result = await agent_client_mod.dispatch_to_project.handler({
        "project_name": "allspark",
        "task": "分析车辆为什么自由导航",
        "context": "已有 allspark session worktree",
        "db_session_id": 88,
        "message_id": 1001,
    })

    assert captured["project_cwd"] == str(worktree)
    assert Path(result["analysis_dir"]).is_dir()
    dispatch_payload = json.loads((session_dir / ".orchestration" / "message-1001" / "dispatch-001.json").read_text(encoding="utf-8"))
    assert dispatch_payload["worktree_path"] == str(worktree)


@pytest.mark.asyncio
async def test_dispatch_to_project_preflight_timeout_keeps_artifacts(tmp_path, monkeypatch):
    from core.artifacts.session_init import initialize_session_workspace
    from core.orchestrator import agent_client as agent_client_mod
    from core.app import project_workspace as project_workspace_mod
    import core.projects as projects_mod

    monkeypatch.setattr(agent_client_mod, "PROJECT_ROOT", tmp_path)
    db_dir = tmp_path / "data" / "db"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "app.sqlite"
    session_dir = tmp_path / "data" / "sessions" / "session-88"
    initialize_session_workspace(session_dir / "workspace", 88)

    async with aiosqlite.connect(db_path) as db:
        await db.executescript(_SCHEMA)
        await db.execute(
            "INSERT INTO sessions (id, project, agent_runtime, agent_session_id, analysis_mode, analysis_workspace, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (88, "allspark", "codex", "orchestrator-session", 0, str(session_dir), datetime.now().isoformat()),
        )
        await db.commit()

    project = SimpleNamespace(name="allspark", path=tmp_path / "repo" / "allspark", description="backend")
    project.path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(projects_mod, "get_project", lambda name: project if name == "allspark" else None)
    monkeypatch.setattr(
        projects_mod,
        "merge_skills",
        lambda *args, **kwargs: {"riot-log-triage": SimpleNamespace(description="triage", prompt="triage")},
    )

    async def instant_timeout(awaitable, timeout):
        awaitable.close()
        raise TimeoutError

    def never_called(*args, **kwargs):
        raise AssertionError("project agent should not run after preflight timeout")

    monkeypatch.setattr(project_workspace_mod, "prepare_project_runtime_context", never_called)
    monkeypatch.setattr(agent_client_mod.agent_client, "get_active_runtime", lambda: "codex")
    monkeypatch.setattr(agent_client_mod.agent_client, "run_for_project", never_called)
    monkeypatch.setattr(agent_client_mod.dispatch_to_project.handler.__globals__["asyncio"], "wait_for", instant_timeout)

    result = await agent_client_mod.dispatch_to_project.handler({
        "project_name": "allspark",
        "task": "检查 B-3T-1 SeerAGV 为什么直接自由导航到 5001",
        "context": "前序已上传日志和截图",
        "skill": "riot-log-triage",
        "db_session_id": 88,
        "message_id": 1001,
    })

    assert "preflight timed out" in result["error"]
    analysis_dir = Path(result["analysis_dir"])
    assert (analysis_dir / "input.json").is_file()
    assert (analysis_dir / "result.json").is_file()
    assert (analysis_dir / "analysis_trace.md").is_file()

    dispatch_payload = json.loads((session_dir / ".orchestration" / "message-1001" / "dispatch-001.json").read_text(encoding="utf-8"))
    assert dispatch_payload["status"] == "preflight_timeout"
    assert dispatch_payload["input_path"].endswith("input.json")

    triage_dir = Path(result["triage_dir"])
    assert (triage_dir / "00-state.json").is_file()
    assert (triage_dir / "01-intake" / "messages" / "dispatch_input.json").is_file()

    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute("SELECT status, input_path, output_path, error_message FROM agent_runs WHERE id = 1")
        row = await cursor.fetchone()
    assert row[0] == "failed"
    assert row[1].endswith("input.json")
    assert row[2].endswith("result.json")
    assert "preflight timed out" in row[3]


@pytest.mark.asyncio
async def test_dispatch_to_project_passes_session_upload_images_to_project_agent(tmp_path, monkeypatch):
    from core.artifacts.session_init import initialize_session_workspace
    from core.orchestrator import agent_client as agent_client_mod
    from core.app import project_workspace as project_workspace_mod
    import core.projects as projects_mod

    monkeypatch.setattr(agent_client_mod, "PROJECT_ROOT", tmp_path)
    db_dir = tmp_path / "data" / "db"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "app.sqlite"

    session_dir = tmp_path / "data" / "sessions" / "session-88"
    initialize_session_workspace(session_dir / "workspace", 88)
    image_path = session_dir / "uploads" / "message-10_media-1.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nimage")
    (session_dir / "uploads" / "message-11_log.txt").write_text("not image", encoding="utf-8")

    async with aiosqlite.connect(db_path) as db:
        await db.executescript(_SCHEMA)
        await db.execute(
            "INSERT INTO sessions (id, project, agent_runtime, agent_session_id, analysis_mode, analysis_workspace, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (88, "allspark", "codex", "orchestrator-session", 0, str(session_dir), datetime.now().isoformat()),
        )
        await db.commit()

    project = SimpleNamespace(name="allspark", path=tmp_path / "repo" / "allspark", description="backend")
    project.path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(projects_mod, "get_project", lambda name: project if name == "allspark" else None)
    monkeypatch.setattr(projects_mod, "merge_skills", lambda *args, **kwargs: {})

    def fake_prepare_project_runtime_context(project_name, ones_result=None, worktree_root=None, worktree_token=""):
        execution_path = Path(worktree_root) / project_name / "stable-main"
        execution_path.mkdir(parents=True, exist_ok=True)
        return SimpleNamespace(
            execution_path=execution_path,
            to_payload=lambda: {
                "running_project": project_name,
                "project_path": str(project.path),
                "execution_path": str(execution_path),
                "worktree_path": str(execution_path),
                "checkout_ref": "main",
                "execution_commit_sha": "sha",
                "execution_version": "test-version",
            },
        )

    monkeypatch.setattr(project_workspace_mod, "prepare_project_runtime_context", fake_prepare_project_runtime_context)

    captured: dict[str, object] = {}

    async def fake_run_for_project(**kwargs):
        captured.update(kwargs)
        return {"text": "project result", "session_id": "project-session", "cost_usd": 0.0}

    monkeypatch.setattr(agent_client_mod.agent_client, "get_active_runtime", lambda: "codex")
    monkeypatch.setattr(agent_client_mod.agent_client, "run_for_project", fake_run_for_project)

    result = await agent_client_mod.dispatch_to_project.handler({
        "project_name": "allspark",
        "task": "结合前序截图分析 SeerAGV 执行任务错误",
        "context": "当前消息没有新图片，但同 session 前序有截图",
        "db_session_id": 88,
        "message_id": 1001,
    })

    expected_image = str(image_path.resolve())
    assert captured["image_paths"] == [expected_image]
    assert expected_image in captured["prompt"]
    input_payload = json.loads((Path(result["analysis_dir"]) / "input.json").read_text(encoding="utf-8"))
    assert input_payload["image_paths"] == [expected_image]
    dispatch_payload = json.loads((session_dir / ".orchestration" / "message-1001" / "dispatch-001.json").read_text(encoding="utf-8"))
    assert dispatch_payload["image_paths"] == [expected_image]


@pytest.mark.asyncio
async def test_dispatch_to_project_keeps_agent_sessions_project_scoped(tmp_path, monkeypatch):
    from core.artifacts.session_init import initialize_session_workspace
    from core.orchestrator import agent_client as agent_client_mod
    from core.app import project_workspace as project_workspace_mod
    import core.projects as projects_mod

    monkeypatch.setattr(agent_client_mod, "PROJECT_ROOT", tmp_path)
    db_dir = tmp_path / "data" / "db"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "app.sqlite"

    session_dir = tmp_path / "data" / "sessions" / "session-88"
    initialize_session_workspace(session_dir / "workspace", 88)

    async with aiosqlite.connect(db_path) as db:
        await db.executescript(_SCHEMA)
        await db.execute(
            "INSERT INTO sessions (id, project, agent_runtime, agent_session_id, analysis_mode, analysis_workspace, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (88, "", "codex", "orchestrator-session", 0, str(session_dir), datetime.now().isoformat()),
        )
        await db.commit()

    projects = {
        "allspark": SimpleNamespace(name="allspark", path=tmp_path / "repo" / "allspark", description="backend"),
        "riot-frontend-v3": SimpleNamespace(name="riot-frontend-v3", path=tmp_path / "repo" / "riot-frontend-v3", description="frontend"),
    }
    for project in projects.values():
        project.path.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(projects_mod, "get_project", lambda name: projects.get(name))
    monkeypatch.setattr(
        projects_mod,
        "merge_skills",
        lambda *args, **kwargs: {},
    )

    def fake_prepare_project_runtime_context(project_name, ones_result=None, worktree_root=None, worktree_token=""):
        execution_path = Path(worktree_root) / project_name / "stable-main"
        execution_path.mkdir(parents=True, exist_ok=True)
        return SimpleNamespace(
            execution_path=execution_path,
            to_payload=lambda: {
                "running_project": project_name,
                "project_path": str(projects[project_name].path),
                "execution_path": str(execution_path),
                "worktree_path": str(execution_path),
                "checkout_ref": "main",
                "execution_commit_sha": f"{project_name}-sha",
                "execution_version": "test-version",
            },
        )

    monkeypatch.setattr(
        project_workspace_mod,
        "prepare_project_runtime_context",
        fake_prepare_project_runtime_context,
    )

    calls: list[dict[str, object]] = []
    returned = iter(["allspark-session-1", "frontend-session-1", "allspark-session-2"])

    async def fake_run_for_project(**kwargs):
        calls.append(kwargs)
        return {"text": "project result", "session_id": next(returned), "cost_usd": 0.0}

    monkeypatch.setattr(agent_client_mod.agent_client, "get_active_runtime", lambda: "codex")
    monkeypatch.setattr(agent_client_mod.agent_client, "run_for_project", fake_run_for_project)

    first = await agent_client_mod.dispatch_to_project.handler({
        "project_name": "allspark",
        "task": "分析后端",
        "session_id": "orchestrator-session",
        "db_session_id": 88,
    })
    second = await agent_client_mod.dispatch_to_project.handler({
        "project_name": "riot-frontend-v3",
        "task": "分析前端",
        "session_id": "orchestrator-session",
        "db_session_id": 88,
    })
    third = await agent_client_mod.dispatch_to_project.handler({
        "project_name": "allspark",
        "task": "继续分析后端",
        "db_session_id": 88,
    })

    assert calls[0]["session_id"] is None
    assert first["resume_source"] == ""
    assert calls[1]["session_id"] is None
    assert second["resume_source"] == ""
    assert calls[2]["session_id"] is None
    assert third["resume_source"] == ""

    payload = json.loads((session_dir / "project_workspace.json").read_text(encoding="utf-8"))
    assert payload["agent_sessions"]["projects"] == {}
    project_runs = payload["project_agent_runs"]
    assert [item["agent_session_id"] for item in project_runs["allspark"]] == [
        "allspark-session-1",
        "allspark-session-2",
    ]
    assert project_runs["riot-frontend-v3"][0]["agent_session_id"] == "frontend-session-1"
    assert Path(first["analysis_dir"]).is_dir()
    assert Path(first["result_path"]).is_file()
    assert Path(second["analysis_dir"]).is_dir()
    assert Path(third["analysis_dir"]).is_dir()

    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute("SELECT agent_session_id, project FROM sessions WHERE id = 88")
        row = await cursor.fetchone()
    assert row[0] == "orchestrator-session"
    assert row[1] == "allspark"


@pytest.mark.asyncio
async def test_dispatch_to_project_accepts_explicit_riot_log_triage_skill(tmp_path, monkeypatch):
    from core.artifacts.session_init import initialize_session_workspace
    from core.orchestrator import agent_client as agent_client_mod
    from core.app import project_workspace as project_workspace_mod
    import core.projects as projects_mod

    monkeypatch.setattr(agent_client_mod, "PROJECT_ROOT", tmp_path)
    db_dir = tmp_path / "data" / "db"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "app.sqlite"
    session_dir = tmp_path / "data" / "sessions" / "session-88"
    initialize_session_workspace(session_dir / "workspace", 88)

    async with aiosqlite.connect(db_path) as db:
        await db.executescript(_SCHEMA)
        await db.execute(
            "INSERT INTO sessions (id, project, agent_runtime, agent_session_id, analysis_mode, analysis_workspace, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (88, "allspark", "codex", "orchestrator-session", 0, str(session_dir), datetime.now().isoformat()),
        )
        await db.commit()

    project = SimpleNamespace(name="allspark", path=tmp_path / "repo" / "allspark", description="backend")
    project.path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(projects_mod, "get_project", lambda name: project if name == "allspark" else None)
    monkeypatch.setattr(
        projects_mod,
        "merge_skills",
        lambda *args, **kwargs: {
            "ones": SimpleNamespace(description="ones", prompt="ones prompt"),
            "riot-log-triage": SimpleNamespace(description="triage", prompt="triage prompt"),
            "other": SimpleNamespace(description="other", prompt="other prompt"),
        },
    )

    def fake_prepare_project_runtime_context(project_name, ones_result=None, worktree_root=None, worktree_token=""):
        execution_path = Path(worktree_root) / project_name / "stable-main"
        execution_path.mkdir(parents=True, exist_ok=True)
        return SimpleNamespace(
            execution_path=execution_path,
            to_payload=lambda: {
                "running_project": project_name,
                "project_path": str(project.path),
                "execution_path": str(execution_path),
                "worktree_path": str(execution_path),
                "checkout_ref": "main",
                "execution_commit_sha": "sha",
                "execution_version": "test-version",
            },
        )

    monkeypatch.setattr(project_workspace_mod, "prepare_project_runtime_context", fake_prepare_project_runtime_context)

    captured: dict[str, object] = {}

    async def fake_run_for_project(**kwargs):
        captured.update(kwargs)
        return {"text": "triage result", "session_id": "triage-project-session", "cost_usd": 0.0}

    monkeypatch.setattr(agent_client_mod.agent_client, "get_active_runtime", lambda: "codex")
    monkeypatch.setattr(agent_client_mod.agent_client, "run_for_project", fake_run_for_project)

    result = await agent_client_mod.dispatch_to_project.handler({
        "project_name": "allspark",
        "task": "分析 ONES 工单 #150745 的订单执行问题",
        "context": "ONES 工单 + 现场日志排障",
        "skill": "riot-log-triage",
        "db_session_id": 88,
    })

    assert result["session_id"] == "triage-project-session"
    assert captured["skill"] == "riot-log-triage"
    assert set(captured["project_agents"]) == {"riot-log-triage", "other"}
    assert "必须使用 riot-log-triage workflow skill" in captured["prompt"]

    payload = json.loads((session_dir / "project_workspace.json").read_text(encoding="utf-8"))
    assert payload["agent_sessions"]["projects"] == {}
    saved = payload["project_agent_runs"]["allspark"][0]
    assert saved["agent_session_id"] == "triage-project-session"
    assert saved["skill"] == "riot-log-triage"
    assert saved["status"] == "success"
    result_payload = json.loads(Path(result["result_path"]).read_text(encoding="utf-8"))
    assert result_payload["project_agent_session_record_only"] is True
    assert result_payload["project_agent_session_id"] == "triage-project-session"
    input_payload = json.loads((Path(result["analysis_dir"]) / "input.json").read_text(encoding="utf-8"))
    assert input_payload["skill"] == "riot-log-triage"


@pytest.mark.asyncio
async def test_dispatch_to_project_does_not_resume_plain_project_session_for_requested_skill(tmp_path, monkeypatch):
    from core.artifacts.session_init import initialize_session_workspace
    from core.orchestrator import agent_client as agent_client_mod
    from core.app import project_workspace as project_workspace_mod
    import core.projects as projects_mod

    monkeypatch.setattr(agent_client_mod, "PROJECT_ROOT", tmp_path)
    db_dir = tmp_path / "data" / "db"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "app.sqlite"
    session_dir = tmp_path / "data" / "sessions" / "session-88"
    initialize_session_workspace(session_dir / "workspace", 88)

    async with aiosqlite.connect(db_path) as db:
        await db.executescript(_SCHEMA)
        await db.execute(
            "INSERT INTO sessions (id, project, agent_runtime, agent_session_id, analysis_mode, analysis_workspace, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (88, "allspark", "codex", "orchestrator-session", 0, str(session_dir), datetime.now().isoformat()),
        )
        await db.commit()

    project = SimpleNamespace(name="allspark", path=tmp_path / "repo" / "allspark", description="backend")
    project.path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(projects_mod, "get_project", lambda name: project if name == "allspark" else None)
    monkeypatch.setattr(
        projects_mod,
        "merge_skills",
        lambda *args, **kwargs: {
            "riot-log-triage": SimpleNamespace(description="triage", prompt="triage prompt"),
        },
    )

    def fake_prepare_project_runtime_context(project_name, ones_result=None, worktree_root=None, worktree_token=""):
        execution_path = Path(worktree_root) / project_name / "stable-main"
        execution_path.mkdir(parents=True, exist_ok=True)
        return SimpleNamespace(
            execution_path=execution_path,
            to_payload=lambda: {
                "running_project": project_name,
                "project_path": str(project.path),
                "execution_path": str(execution_path),
                "worktree_path": str(execution_path),
                "checkout_ref": "main",
                "execution_commit_sha": "sha",
                "execution_version": "test-version",
            },
        )

    monkeypatch.setattr(project_workspace_mod, "prepare_project_runtime_context", fake_prepare_project_runtime_context)

    returned = iter(["plain-project-session", "triage-project-session"])
    calls: list[dict[str, object]] = []

    async def fake_run_for_project(**kwargs):
        calls.append(kwargs)
        return {"text": "result", "session_id": next(returned), "cost_usd": 0.0}

    monkeypatch.setattr(agent_client_mod.agent_client, "get_active_runtime", lambda: "codex")
    monkeypatch.setattr(agent_client_mod.agent_client, "run_for_project", fake_run_for_project)

    await agent_client_mod.dispatch_to_project.handler({
        "project_name": "allspark",
        "task": "普通项目咨询",
        "db_session_id": 88,
    })
    await agent_client_mod.dispatch_to_project.handler({
        "project_name": "allspark",
        "task": "分析日志排障",
        "skill": "riot-log-triage",
        "db_session_id": 88,
    })

    assert calls[0]["session_id"] is None
    assert calls[0]["skill"] is None
    assert calls[1]["session_id"] is None
    assert calls[1]["skill"] == "riot-log-triage"


@pytest.mark.asyncio
async def test_dispatch_to_project_uses_session_agent_runtime_when_contextvar_is_default(tmp_path, monkeypatch):
    from core.artifacts.session_init import initialize_session_workspace
    from core.orchestrator import agent_client as agent_client_mod
    from core.app import project_workspace as project_workspace_mod
    import core.projects as projects_mod

    monkeypatch.setattr(agent_client_mod, "PROJECT_ROOT", tmp_path)
    db_dir = tmp_path / "data" / "db"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "app.sqlite"
    session_dir = tmp_path / "data" / "sessions" / "session-88"
    initialize_session_workspace(session_dir / "workspace", 88)

    async with aiosqlite.connect(db_path) as db:
        await db.executescript(_SCHEMA)
        await db.execute(
            "INSERT INTO sessions (id, project, agent_runtime, agent_session_id, analysis_mode, analysis_workspace, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (88, "allspark", "codex", "orchestrator-session", 0, str(session_dir), datetime.now().isoformat()),
        )
        await db.commit()

    project = SimpleNamespace(name="allspark", path=tmp_path / "repo" / "allspark", description="backend")
    project.path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(projects_mod, "get_project", lambda name: project if name == "allspark" else None)
    monkeypatch.setattr(projects_mod, "merge_skills", lambda *args, **kwargs: {})

    def fake_prepare_project_runtime_context(project_name, ones_result=None, worktree_root=None, worktree_token=""):
        execution_path = Path(worktree_root) / project_name / "stable-main"
        execution_path.mkdir(parents=True, exist_ok=True)
        return SimpleNamespace(
            execution_path=execution_path,
            to_payload=lambda: {
                "running_project": project_name,
                "project_path": str(project.path),
                "execution_path": str(execution_path),
                "worktree_path": str(execution_path),
                "checkout_ref": "main",
                "execution_commit_sha": "sha",
                "execution_version": "test-version",
            },
        )

    monkeypatch.setattr(project_workspace_mod, "prepare_project_runtime_context", fake_prepare_project_runtime_context)
    monkeypatch.setattr(agent_client_mod.agent_client, "get_active_runtime", lambda: "claude")

    captured: dict[str, object] = {}

    async def fake_run_for_project(**kwargs):
        captured.update(kwargs)
        return {"text": "project result", "session_id": "project-session", "cost_usd": 0.0}

    monkeypatch.setattr(agent_client_mod.agent_client, "run_for_project", fake_run_for_project)

    result = await agent_client_mod.dispatch_to_project.handler({
        "project_name": "allspark",
        "task": "分析项目",
        "db_session_id": 88,
    })

    assert result["session_id"] == "project-session"
    assert captured["runtime"] == "codex"


@pytest.mark.asyncio
async def test_dispatch_to_project_marks_agent_runtime_error_failed(tmp_path, monkeypatch):
    from core.artifacts.session_init import initialize_session_workspace
    from core.orchestrator import agent_client as agent_client_mod
    from core.app import project_workspace as project_workspace_mod
    import core.projects as projects_mod

    monkeypatch.setattr(agent_client_mod, "PROJECT_ROOT", tmp_path)
    db_dir = tmp_path / "data" / "db"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "app.sqlite"
    session_dir = tmp_path / "data" / "sessions" / "session-88"
    initialize_session_workspace(session_dir / "workspace", 88)

    async with aiosqlite.connect(db_path) as db:
        await db.executescript(_SCHEMA)
        await db.execute(
            "INSERT INTO sessions (id, project, agent_runtime, agent_session_id, analysis_mode, analysis_workspace, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (88, "allspark", "codex", "orchestrator-session", 0, str(session_dir), datetime.now().isoformat()),
        )
        await db.commit()

    project = SimpleNamespace(name="allspark", path=tmp_path / "repo" / "allspark", description="backend")
    project.path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(projects_mod, "get_project", lambda name: project if name == "allspark" else None)
    monkeypatch.setattr(projects_mod, "merge_skills", lambda *args, **kwargs: {})

    def fake_prepare_project_runtime_context(project_name, ones_result=None, worktree_root=None, worktree_token=""):
        execution_path = Path(worktree_root) / project_name / "stable-main"
        execution_path.mkdir(parents=True, exist_ok=True)
        return SimpleNamespace(
            execution_path=execution_path,
            to_payload=lambda: {
                "running_project": project_name,
                "project_path": str(project.path),
                "execution_path": str(execution_path),
                "worktree_path": str(execution_path),
                "checkout_ref": "main",
                "execution_commit_sha": "sha",
                "execution_version": "test-version",
            },
        )

    monkeypatch.setattr(project_workspace_mod, "prepare_project_runtime_context", fake_prepare_project_runtime_context)
    monkeypatch.setattr(agent_client_mod.agent_client, "get_active_runtime", lambda: "claude")

    async def fake_run_for_project(**kwargs):
        return {"text": "Failed to authenticate", "session_id": "project-session", "is_error": True}

    monkeypatch.setattr(agent_client_mod.agent_client, "run_for_project", fake_run_for_project)

    result = await agent_client_mod.dispatch_to_project.handler({
        "project_name": "allspark",
        "task": "分析项目",
        "db_session_id": 88,
    })

    assert result["error"] == "Failed to authenticate"
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT status, error_message FROM agent_runs WHERE session_id = 88 AND agent_name = 'dispatch:allspark'"
        )
        row = await cursor.fetchone()
    assert row[0] == "failed"
    assert row[1] == "Failed to authenticate"


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

    async def fake_run_for_project(**kwargs):
        return {"text": "project result", "session_id": "record-only-session", "cost_usd": 0.0}

    monkeypatch.setattr(agent_client_mod.agent_client, "get_active_runtime", lambda: "codex")
    monkeypatch.setattr(agent_client_mod.agent_client, "run_for_project", fake_run_for_project)

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
