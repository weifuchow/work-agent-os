"""Agent-visible MCP tool definitions and scope registries."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from core.config import settings
from core.orchestrator.dispatch_artifacts import _app_db_path, _project_root, _session_workspace_path
from core.orchestrator.dispatch_service import dispatch_to_project as dispatch_to_project_impl


@tool(
    "query_db",
    "对本地 SQLite 数据库执行只读 SQL 查询。表: messages, sessions, session_messages, tasks, reports, agent_runs, audit_logs, memory_entries。",
    {"type": "object", "properties": {"sql": {"type": "string", "description": "SELECT SQL 语句"}}, "required": ["sql"]},
)
async def query_db(input: dict) -> dict[str, Any]:
    import aiosqlite

    sql = input["sql"].strip()
    if not sql.upper().startswith("SELECT"):
        return {"error": "只允许 SELECT 查询"}
    db_path = _app_db_path()
    try:
        async with aiosqlite.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(sql)
            rows = await cursor.fetchall()
            columns = [desc[0] for desc in cursor.description] if cursor.description else []
            return {"columns": columns, "rows": [dict(zip(columns, r)) for r in rows], "count": len(rows)}
    except Exception as e:
        return {"error": str(e)}


@tool(
    "read_memory",
    "读取 data/memory 下的记忆文件（项目知识、个人偏好等）。",
    {"type": "object", "properties": {"path": {"type": "string", "description": "相对于 data/memory/ 的路径"}}, "required": ["path"]},
)
async def read_memory(input: dict) -> dict[str, Any]:
    fp = _project_root() / "data" / "memory" / input["path"]
    if not fp.exists():
        return {"error": f"文件不存在: {input['path']}"}
    try:
        return {"path": input["path"], "content": fp.read_text(encoding="utf-8")}
    except Exception as e:
        return {"error": str(e)}


@tool(
    "search_memory_entries",
    "检索结构化长期记忆。可按项目、作用域、类别和关键词查找项目决策、历史问题、解决方案、个人偏好等。",
    {"type": "object", "properties": {
        "q": {"type": "string", "description": "关键词，可匹配标题、正文、标签、项目名"},
        "project_name": {"type": "string", "description": "项目名。传空字符串表示只看非项目记忆"},
        "project_version": {"type": "string", "description": "项目版本号，如 3.0、v4.8.0.6"},
        "scope": {"type": "string", "enum": ["project", "personal", "people", "general"]},
        "category": {"type": "string", "enum": ["decision", "milestone", "issue", "solution", "preference", "person", "fact", "note"]},
        "limit": {"type": "integer", "description": "返回条数，默认 10，最大 20"},
    }, "required": []},
)
async def search_memory_entries(input: dict) -> dict[str, Any]:
    from core.database import async_session_factory
    from core.memory.store import ensure_memory_bootstrap, memory_entry_to_dict, search_memory_entries as _search

    try:
        async with async_session_factory() as db:
            await ensure_memory_bootstrap(db)
            entries = await _search(
                db,
                limit=input.get("limit", 10),
                project_name=input.get("project_name"),
                scope=input.get("scope"),
                category=input.get("category"),
                q=" ".join(part for part in [input.get("q"), input.get("project_version")] if part),
            )
            return {"items": [memory_entry_to_dict(entry) for entry in entries], "count": len(entries)}
    except Exception as e:
        return {"error": str(e)}


@tool(
    "list_projects",
    "列出所有已注册的项目（名称 + 描述），用于判断将消息路由到哪个项目。",
    {"type": "object", "properties": {}, "required": []},
)
async def list_projects_tool(input: dict) -> dict[str, Any]:
    from core.projects import get_projects

    projects = get_projects()
    return {
        "projects": [
            {"name": p.name, "path": str(p.path), "description": p.description}
            for p in projects
        ]
    }


@tool(
    "prepare_project_worktree",
    "按需把已注册项目加载为当前 session 下的 worktree，并写入 project_workspace 注册表。"
    "必须提供 project_name；优先提供 workspace/input/project_context.json 中的 session_workspace_path。",
    {"type": "object", "properties": {
        "project_name": {"type": "string", "description": "projects.yaml 中注册的项目名称"},
        "session_workspace_path": {"type": "string", "description": "session_workspace.json 的绝对路径"},
        "db_session_id": {"type": "integer", "description": "DB 会话 ID；没有 session_workspace_path 时用于推导 session 路径"},
        "reason": {"type": "string", "description": "加载原因，例如涉及单机本体/前端/部署包"},
        "active": {"type": "boolean", "description": "是否设为 active_project，默认 true"},
    }, "required": ["project_name"]},
)
async def prepare_project_worktree_tool(input: dict) -> dict[str, Any]:
    from core.app.project_workspace import prepare_project_from_session_workspace_path

    project_name = str(input.get("project_name") or "").strip()
    if not project_name:
        return {"error": "project_name is required"}

    session_workspace_text = str(input.get("session_workspace_path") or "").strip()
    session_workspace_path = Path(session_workspace_text) if session_workspace_text else None
    if not session_workspace_path and input.get("db_session_id"):
        session_workspace_path = _session_workspace_path(int(input["db_session_id"]))
    if not session_workspace_path:
        return {"error": "session_workspace_path or db_session_id is required"}
    if not session_workspace_path.exists():
        return {"error": f"session workspace not found: {session_workspace_path}"}

    entry = prepare_project_from_session_workspace_path(
        project_name,
        session_workspace_path=session_workspace_path,
        reason=str(input.get("reason") or "on_demand"),
        active=bool(input.get("active", True)),
    )
    if not entry:
        return {"error": f"failed to prepare project worktree: {project_name}"}
    return {
        "status": "ready",
        "project": project_name,
        "worktree_path": entry.get("worktree_path") or entry.get("execution_path") or "",
        "checkout_ref": entry.get("checkout_ref") or "",
        "execution_version": entry.get("execution_version") or "",
        "execution_commit_sha": entry.get("execution_commit_sha") or "",
        "project_workspace_path": str(session_workspace_path.parent / "project_workspace.json"),
        "entry": entry,
    }


@tool(
    "dispatch_to_project",
    "将任务派发到指定项目的 Agent，在该项目目录下执行，使用该项目的 skills。"
    "project_name 必须由主编排根据当前用户问题与上下文关联度决定；skill 应由主编排显式传入。"
    "若主编排漏传，且当前 RIOT 项目任务明显属于日志、ONES、订单/车辆执行链路、截图或附件排障，本工具会兜底选择 riot-log-triage。"
    "本工具不会自动恢复项目 Agent session；session_id 仅作为审计兼容字段保存，不作为 resume 输入。"
    "主编排判断本轮属于 ONES 工单、现场日志、订单/车辆执行链路、截图或附件排障时，应传 skill='riot-log-triage'。"
    "只有用户明确要求生成文件，或需要完整技术方案/方案设计/可交付文档时，才传 target_artifacts/artifact_instruction；"
    "target_artifacts 必须由主编排预先定义固定文件名和输出路径，项目 Agent 只能按这些 path 生成或完善文件；"
    "用户要求多个产物时，target_artifacts 要逐个列出，例如方案 A、方案 B 对应两个独立条目；"
    "普通分析不要要求项目 Agent 生成与卡片内容重复的附件。"
    "每次调用都会创建项目分析目录，并返回项目 Agent 输出、分析目录和 record-only session_id。",
    {"type": "object", "properties": {
        "project_name": {"type": "string", "description": "projects.yaml 中注册的项目名称"},
        "task": {"type": "string", "description": "要派发给项目 Agent 的任务描述（详细）"},
        "context": {"type": "string", "description": "消息的原始内容和背景信息，供项目 Agent 参考"},
        "target_artifacts": {
            "type": "array",
            "description": "可选：主编排预先定义的固定目标文件列表；普通分析不要传。项目 Agent 只能生成这些 path",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "主编排固定下发的输出路径/文件名，项目 Agent 不能改名"},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                },
            },
        },
        "artifact_instruction": {"type": "string", "description": "可选：目标产物要求和边界；没有明确文件产物时不要传"},
        "skill": {"type": "string", "description": "可选：指定项目 Agent 必须加载的 workflow skill，例如 riot-log-triage"},
        "session_id": {"type": "string", "description": "可选兼容字段，仅审计记录；不会用于项目 Agent resume。不要传 workspace/input/session.json 里的全局 agent_session_id"},
        "db_session_id": {"type": "integer", "description": "DB 会话 ID，用于读取/持久化项目级 Agent session"},
        "message_id": {"type": "integer", "description": "可选：当前 DB 消息 ID，用于落盘目录 message-<id>"},
        "orchestration_turn_id": {"type": "string", "description": "可选：主编排本轮 ID，用于审计"},
    }, "required": ["project_name", "task"]},
)
async def dispatch_to_project(input: dict) -> dict[str, Any]:
    return await dispatch_to_project_impl(input)


CUSTOM_TOOLS = [query_db, list_projects_tool, prepare_project_worktree_tool, dispatch_to_project]
if settings.enable_memory_tools:
    CUSTOM_TOOLS.extend([read_memory, search_memory_entries])

PROJECT_TOOLS = [query_db]
if settings.enable_memory_tools:
    PROJECT_TOOLS.extend([read_memory, search_memory_entries])

CUSTOM_MCP_SERVER = create_sdk_mcp_server(
    name="work-agent-tools",
    version="1.0.0",
    tools=CUSTOM_TOOLS,
)

PROJECT_MCP_SERVER = create_sdk_mcp_server(
    name="work-agent-tools",
    version="1.0.0",
    tools=PROJECT_TOOLS,
)

CUSTOM_TOOL_NAMES = [f"mcp__work-agent-tools__{t.name}" for t in CUSTOM_TOOLS]
PROJECT_TOOL_NAMES = [f"mcp__work-agent-tools__{t.name}" for t in PROJECT_TOOLS]

ORCHESTRATOR_TOOLS = [query_db, prepare_project_worktree_tool, dispatch_to_project]
if settings.enable_memory_tools:
    ORCHESTRATOR_TOOLS.extend([read_memory, search_memory_entries])

ORCHESTRATOR_MCP_SERVER = create_sdk_mcp_server(
    name="work-agent-tools",
    version="1.0.0",
    tools=ORCHESTRATOR_TOOLS,
)

ORCHESTRATOR_TOOL_NAMES = [f"mcp__work-agent-tools__{t.name}" for t in ORCHESTRATOR_TOOLS]
