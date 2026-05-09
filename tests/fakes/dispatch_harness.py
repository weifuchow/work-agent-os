from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aiosqlite

from core.artifacts.session_init import InitializedSessionWorkspace, initialize_session_workspace


SCHEMA = """
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


class DispatchHarness:
    def __init__(
        self,
        *,
        tmp_path: Path,
        db_path: Path,
        session_dir: Path,
        initialized: InitializedSessionWorkspace | None,
        session_id: int,
    ) -> None:
        self.tmp_path = tmp_path
        self.db_path = db_path
        self.session_dir = session_dir
        self.initialized = initialized
        self.session_id = session_id

    async def dispatch(self, **payload: Any) -> dict[str, Any]:
        from core.orchestrator import agent_client as agent_client_mod

        return await agent_client_mod.dispatch_to_project.handler(payload)

    def dispatch_payload(self, message_id: int, dispatch_id: str = "dispatch-001") -> dict[str, Any]:
        return read_json(self.session_dir / ".orchestration" / f"message-{message_id}" / f"{dispatch_id}.json")

    def project_workspace(self) -> dict[str, Any]:
        return read_json(self.session_dir / "project_workspace.json")

    async def agent_run_row(self, sql: str, params: tuple[Any, ...] = ()) -> tuple[Any, ...]:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(sql, params)
            row = await cursor.fetchone()
        return tuple(row) if row else ()


async def make_dispatch_session(
    tmp_path: Path,
    monkeypatch: Any,
    *,
    session_id: int = 88,
    project: str = "allspark",
    runtime: str = "codex",
    agent_session_id: str = "orchestrator-session",
    analysis_mode: int = 0,
    analysis_workspace: str | Path | None = None,
    create_workspace: bool = True,
) -> DispatchHarness:
    from core.orchestrator import agent_client as agent_client_mod

    monkeypatch.setattr(agent_client_mod, "PROJECT_ROOT", tmp_path)
    db_path = tmp_path / "data" / "db" / "app.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    session_dir = tmp_path / "data" / "sessions" / f"session-{session_id}"
    initialized = initialize_session_workspace(session_dir / "workspace", session_id) if create_workspace else None
    workspace_text = str(analysis_workspace if analysis_workspace is not None else (session_dir if create_workspace else ""))

    async with aiosqlite.connect(db_path) as db:
        await db.executescript(SCHEMA)
        await db.execute(
            "INSERT INTO sessions (id, project, agent_runtime, agent_session_id, analysis_mode, analysis_workspace, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (session_id, project, runtime, agent_session_id, analysis_mode, workspace_text, "2026-05-09T00:00:00"),
        )
        await db.commit()

    return DispatchHarness(
        tmp_path=tmp_path,
        db_path=db_path,
        session_dir=session_dir,
        initialized=initialized,
        session_id=session_id,
    )


def skill_defs(*names: str) -> dict[str, SimpleNamespace]:
    return {name: SimpleNamespace(description=name, prompt=f"{name} prompt") for name in names}


def install_project_skill(tmp_path: Path, name: str = "riot-log-triage") -> Path:
    skill_dir = tmp_path / ".claude" / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: test skill\n---\n\n# {name}\n",
        encoding="utf-8",
    )
    return skill_dir


def register_project(
    monkeypatch: Any,
    tmp_path: Path,
    *,
    projects: tuple[str, ...] = ("allspark",),
    skills: dict[str, Any] | None = None,
) -> dict[str, SimpleNamespace]:
    from core.app import project_workspace as project_workspace_mod
    import core.projects as projects_mod

    registered = {
        name: SimpleNamespace(
            name=name,
            path=tmp_path / "repo" / name,
            description="frontend" if "frontend" in name else "backend",
        )
        for name in projects
    }
    for project in registered.values():
        project.path.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(projects_mod, "get_project", lambda name: registered.get(name))
    monkeypatch.setattr(projects_mod, "merge_skills", lambda *args, **kwargs: skills or {})

    def fake_project_runtime(
        project_name: str,
        ones_result: dict[str, Any] | None = None,
        worktree_root: str | Path | None = None,
        worktree_token: str = "",
    ) -> SimpleNamespace:
        base = Path(worktree_root) if worktree_root else registered[project_name].path
        execution_path = base / project_name / "stable-main" if worktree_root else base
        execution_path.mkdir(parents=True, exist_ok=True)
        return SimpleNamespace(
            execution_path=execution_path,
            to_payload=lambda: {
                "running_project": project_name,
                "project_path": str(registered[project_name].path),
                "execution_path": str(execution_path),
                "worktree_path": str(execution_path),
                "checkout_ref": "main",
                "execution_commit_sha": f"{project_name}-sha",
                "execution_version": "test-version",
            },
        )

    monkeypatch.setattr(project_workspace_mod, "prepare_project_runtime_context", fake_project_runtime)
    return registered


def capture_project_agent_run(
    monkeypatch: Any,
    *,
    session_ids: str | list[str] = "project-session",
    text: str = "project result",
    is_error: bool = False,
    runtime: str = "codex",
) -> list[dict[str, Any]]:
    from core.orchestrator import agent_client as agent_client_mod

    calls: list[dict[str, Any]] = []
    returned = iter(session_ids if isinstance(session_ids, list) else [session_ids])

    async def fake_run_for_project(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {
            "text": text,
            "session_id": next(returned),
            "cost_usd": 0.0,
            **({"is_error": True} if is_error else {}),
        }

    monkeypatch.setattr(agent_client_mod.agent_client, "get_active_runtime", lambda: runtime)
    monkeypatch.setattr(agent_client_mod.agent_client, "run_for_project", fake_run_for_project)
    return calls


def simulate_preflight_timeout(monkeypatch: Any) -> None:
    from core.orchestrator import dispatch_service as dispatch_service_mod

    async def instant_timeout(awaitable: Any, timeout: int) -> None:
        close = getattr(awaitable, "close", None)
        if close:
            close()
        raise TimeoutError

    monkeypatch.setattr(dispatch_service_mod.asyncio, "wait_for", instant_timeout)


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
