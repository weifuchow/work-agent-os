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
