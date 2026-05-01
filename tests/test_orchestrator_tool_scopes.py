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
