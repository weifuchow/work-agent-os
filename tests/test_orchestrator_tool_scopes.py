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
    assert _tool_names(tools.ORCHESTRATOR_TOOLS) >= {"query_db", "dispatch_to_project"}
    assert _tool_names(tools.PROJECT_TOOLS) >= {"query_db"}
    assert "dispatch_to_project" not in _tool_names(tools.PROJECT_TOOLS)


def test_orchestrator_runtime_does_not_expose_project_execution_tools():
    from core.orchestrator.agent_client import AgentClient

    opts = AgentClient()._build_options(orchestrator_mode=True)

    forbidden = {"Read", "Write", "Edit", "Bash", "Glob", "Grep"}
    assert not (set(opts.allowed_tools or []) & forbidden)
    assert "mcp__work-agent-tools__dispatch_to_project" in (opts.allowed_tools or [])


def test_project_runtime_keeps_project_execution_tools_without_recursive_dispatch():
    from core.orchestrator.agent_client import AgentClient

    opts = AgentClient()._build_options(orchestrator_mode=False, project_agents={})

    expected = {"Read", "Write", "Edit", "Bash", "Glob", "Grep"}
    assert expected <= set(opts.allowed_tools or [])
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
