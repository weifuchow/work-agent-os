from core.orchestrator import tools


def _tool_names(items):
    return {item.name for item in items}


def test_platform_side_effect_tools_are_not_exposed_to_agents():
    side_effect_tools = {
        "send_feishu_message",
        "reply_to_message",
        "save_bot_reply",
        "update_session",
        "link_task_context",
        "write_audit_log",
        "write_memory",
        "upsert_memory_entry",
    }

    assert not (_tool_names(tools.CUSTOM_TOOLS) & side_effect_tools)
    assert not (_tool_names(tools.PROJECT_TOOLS) & side_effect_tools)
    assert not (_tool_names(tools.ORCHESTRATOR_TOOLS) & side_effect_tools)


def test_agent_tool_scopes_keep_only_read_and_dispatch_capabilities():
    assert _tool_names(tools.ORCHESTRATOR_TOOLS) >= {
        "query_db",
        "prepare_project_worktree",
        "dispatch_to_project",
    }
    assert _tool_names(tools.PROJECT_TOOLS) >= {"query_db"}
    assert "prepare_project_worktree" not in _tool_names(tools.PROJECT_TOOLS)
    assert "dispatch_to_project" not in _tool_names(tools.PROJECT_TOOLS)


def test_orchestrator_runtime_does_not_expose_project_execution_tools():
    from core.orchestrator.agent_client import AgentClient

    opts = AgentClient()._build_options(orchestrator_mode=True)

    forbidden = {"Read", "Write", "Edit", "Bash", "Glob", "Grep"}
    assert not (set(opts.allowed_tools or []) & forbidden)
    assert "mcp__work-agent-tools__prepare_project_worktree" in (opts.allowed_tools or [])
    assert "mcp__work-agent-tools__dispatch_to_project" in (opts.allowed_tools or [])


def test_project_runtime_keeps_project_execution_tools_without_recursive_dispatch():
    from core.orchestrator.agent_client import AgentClient

    opts = AgentClient()._build_options(orchestrator_mode=False, project_agents={})

    expected = {"Read", "Write", "Edit", "Bash", "Glob", "Grep"}
    assert expected <= set(opts.allowed_tools or [])
    assert "mcp__work-agent-tools__prepare_project_worktree" not in (opts.allowed_tools or [])
    assert "mcp__work-agent-tools__dispatch_to_project" not in (opts.allowed_tools or [])


def test_orchestrator_prompt_requires_project_dispatch_boundary():
    from core.agents.runner import GENERIC_SYSTEM_PROMPT, _build_generic_prompt
    from core.ports import AgentRequest

    request = AgentRequest(
        workspace_path=__import__("pathlib").Path("D:/tmp/session/workspace"),
        message={"id": 1, "message_type": "text", "content": "ONES#150738 allspark 日志排查"},
        session={"id": 173, "agent_session_id": "orchestrator-session"},
        history=[],
        skill_registry={"skills": []},
    )
    prompt = _build_generic_prompt(request)

    for text in (GENERIC_SYSTEM_PROMPT, prompt):
        assert "必须调用 dispatch_to_project" in text
        assert "主编排" in text
        assert "不要" in text and "项目分析" in text
        assert "riot-log-triage" in text
        assert "main-orchestrator-project-coordination" in text
        assert "active_project" in text and "历史提示" in text
        assert "project_name" in text
        assert "项目 Agent 返回后" in text
        assert "项目工程目录初始化" in text
        assert "prepare_project_worktree" in text
        assert "项目 Agent 不负责" in text


def test_main_orchestrator_project_coordination_skill_is_discoverable():
    from core.artifacts.manifest import discover_skill_registry

    registry = discover_skill_registry()
    skills = {item["name"]: item for item in registry["skills"]}

    assert "main-orchestrator-project-coordination" in skills
    assert "main-orchestrator-project-routing" not in skills
    trigger = skills["main-orchestrator-project-coordination"]["trigger_summary"]
    assert "dispatch_to_project" in trigger
    assert "项目工程目录初始化" in trigger
    assert "project result review" in trigger or "项目" in trigger
    scripts = skills["main-orchestrator-project-coordination"]["scripts_dir"].replace("\\", "/")
    assert scripts.endswith(".claude/skills/main-orchestrator-project-coordination/scripts")


def test_main_orchestrator_project_coordination_bundles_deterministic_scripts():
    from pathlib import Path

    skill_dir = Path(".claude/skills/main-orchestrator-project-coordination")
    scripts_dir = skill_dir / "scripts"

    expected = {
        "collect_orchestration_context.py",
        "validate_dispatch_artifacts.py",
        "mark_stale_dispatch.py",
        "e2e_scenario.py",
    }
    assert expected <= {path.name for path in scripts_dir.glob("*.py")}

    body = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    for name in expected:
        assert name in body

    responsibility_map = skill_dir / "references" / "responsibility-map.md"
    assert responsibility_map.exists()
    assert "references/responsibility-map.md" in body
    assert "脚本归属" in responsibility_map.read_text(encoding="utf-8")


def test_root_e2e_scenario_script_is_only_compatibility_entrypoint():
    from pathlib import Path

    text = Path("scripts/e2e_scenario.py").read_text(encoding="utf-8")

    assert "runpy.run_path" in text
    assert "main-orchestrator-project-coordination" in text
    assert "process_message" not in text


def test_replay_environment_redirects_db_and_session_roots(tmp_path, monkeypatch):
    db_path = tmp_path / "replay" / "app.sqlite"
    sessions_root = tmp_path / "replay" / "sessions"

    monkeypatch.setenv("WORK_AGENT_REPLAY_DB_PATH", str(db_path))
    monkeypatch.setenv("WORK_AGENT_REPLAY_SESSIONS_DIR", str(sessions_root))

    assert tools._app_db_path() == db_path
    assert tools._sessions_root() == sessions_root
    assert tools._session_workspace_path(175) == sessions_root / "session-175" / "session_workspace.json"


def test_codex_mcp_server_serializes_plain_dict_tool_results(monkeypatch):
    import asyncio
    from mcp.types import CallToolRequest, CallToolRequestParams
    from core.orchestrator import codex_mcp_server

    async def fake_query_db(_input):
        return {"columns": ["ok"], "rows": [{"ok": 1}], "count": 1}

    monkeypatch.setattr(tools.query_db, "handler", fake_query_db)

    server = codex_mcp_server._build_server("orchestrator")
    handler = server.request_handlers[CallToolRequest]
    result = asyncio.run(handler(CallToolRequest(params=CallToolRequestParams(
        name="query_db",
        arguments={"sql": "SELECT 1 AS ok"},
    ))))

    payload = result.root
    assert payload.structuredContent == {"columns": ["ok"], "rows": [{"ok": 1}], "count": 1}
    assert payload.content[0].type == "text"
    assert '"ok": 1' in payload.content[0].text
    assert payload.isError is False
