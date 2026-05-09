"""Compatibility facade for orchestrator MCP tools.

Implementation lives in focused modules:
- dispatch_service.py: project dispatch orchestration
- dispatch_artifacts.py: session/artifact path helpers
- routing_policy.py: hard routing and skill-selection policy
- mcp_tools.py: agent-visible MCP tool registration
"""

from core.orchestrator.dispatch_artifacts import (
    _app_db_path,
    _project_root,
    _session_workspace_path,
    _sessions_root,
)
from core.orchestrator.dispatch_service import PROJECT_AGENT_RESPONSE_RULES
from core.orchestrator.mcp_tools import (
    CUSTOM_MCP_SERVER,
    CUSTOM_TOOL_NAMES,
    CUSTOM_TOOLS,
    ORCHESTRATOR_MCP_SERVER,
    ORCHESTRATOR_TOOL_NAMES,
    ORCHESTRATOR_TOOLS,
    PROJECT_MCP_SERVER,
    PROJECT_TOOL_NAMES,
    PROJECT_TOOLS,
    dispatch_to_project,
    list_projects_tool,
    prepare_project_worktree_tool,
    query_db,
    read_memory,
    search_memory_entries,
)

__all__ = [
    "CUSTOM_MCP_SERVER",
    "CUSTOM_TOOL_NAMES",
    "CUSTOM_TOOLS",
    "ORCHESTRATOR_MCP_SERVER",
    "ORCHESTRATOR_TOOL_NAMES",
    "ORCHESTRATOR_TOOLS",
    "PROJECT_AGENT_RESPONSE_RULES",
    "PROJECT_MCP_SERVER",
    "PROJECT_TOOL_NAMES",
    "PROJECT_TOOLS",
    "_app_db_path",
    "_project_root",
    "_session_workspace_path",
    "_sessions_root",
    "dispatch_to_project",
    "list_projects_tool",
    "prepare_project_worktree_tool",
    "query_db",
    "read_memory",
    "search_memory_entries",
]
