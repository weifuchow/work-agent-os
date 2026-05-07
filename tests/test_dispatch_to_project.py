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
        "session_id": "sdk-project-session",
        "db_session_id": 88,
    })

    assert result["session_id"] == "triage-session-new"
    assert captured["skill"] == "riot-log-triage"
    assert captured["session_id"] == "triage-session-old"
    assert set(captured["project_agents"]) == {"riot-log-triage", "other"}

    updated_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert updated_state["agent_context"]["session_id"] == "triage-session-new"
    assert updated_state["agent_context"]["skill"] == "riot-log-triage"

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
        "session_id": "sdk-project-session",
        "db_session_id": 88,
    })

    assert captured["skill"] == "riot-log-triage"
    assert captured["session_id"] is None


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
    assert calls[2]["session_id"] == "allspark-session-1"
    assert third["resume_source"] == "project_workspace"

    payload = json.loads((session_dir / "project_workspace.json").read_text(encoding="utf-8"))
    project_sessions = payload["agent_sessions"]["projects"]
    assert project_sessions["allspark"]["session_id"] == "allspark-session-2"
    assert project_sessions["riot-frontend-v3"]["session_id"] == "frontend-session-1"

    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute("SELECT agent_session_id, project FROM sessions WHERE id = 88")
        row = await cursor.fetchone()
    assert row[0] == "orchestrator-session"
    assert row[1] == "allspark"
