"""Unified agent runtime client with Claude and Codex support."""

import asyncio
from contextvars import ContextVar
import json
import os
import shutil
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Optional

from loguru import logger

from claude_agent_sdk import (
    ClaudeAgentOptions,
    AgentDefinition,
    HookMatcher,
    SubagentStopHookInput,
    HookContext,
    query,
    tool,
    create_sdk_mcp_server,
    list_sessions as sdk_list_sessions,
    get_session_messages as sdk_get_session_messages,
    delete_session as sdk_delete_session,
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
)

from core.config import (
    settings,
    get_agent_runtime_override,
    get_model_override,
    load_models_config,
)
from core.orchestrator.agent_runtime import (
    DEFAULT_AGENT_RUNTIME,
    get_agent_run_runtime_type,
    normalize_agent_runtime,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
_ACTIVE_AGENT_RUNTIME: ContextVar[str] = ContextVar(
    "active_agent_runtime",
    default=DEFAULT_AGENT_RUNTIME,
)
_NPM_CODEX_EXE = (
    Path(os.environ.get("APPDATA", ""))
    / "npm"
    / "node_modules"
    / "@openai"
    / "codex"
    / "node_modules"
    / "@openai"
    / "codex-win32-x64"
    / "vendor"
    / "x86_64-pc-windows-msvc"
    / "codex"
    / "codex.exe"
)


# ==================== Custom Tools ====================

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
    db_path = PROJECT_ROOT / "data" / "db" / "app.sqlite"
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
    "write_audit_log",
    "写入审计日志。",
    {"type": "object", "properties": {"event_type": {"type": "string"}, "target_type": {"type": "string"}, "target_id": {"type": "string"}, "detail": {"type": "string"}}, "required": ["event_type", "detail"]},
)
async def write_audit_log(input: dict) -> dict[str, Any]:
    import aiosqlite
    from datetime import datetime, UTC
    db_path = PROJECT_ROOT / "data" / "db" / "app.sqlite"
    try:
        async with aiosqlite.connect(str(db_path)) as db:
            await db.execute(
                "INSERT INTO audit_logs (event_type, target_type, target_id, detail, operator, created_at) VALUES (?,?,?,?,?,?)",
                (input["event_type"], input.get("target_type", ""), input.get("target_id", ""), input["detail"], "agent", datetime.now().isoformat()),
            )
            await db.commit()
            return {"success": True}
    except Exception as e:
        return {"error": str(e)}


@tool(
    "send_feishu_message",
    "通过飞书发送消息到 chat_id（仅用于首次主动发消息，不创建话题）。回复用户消息请优先用 reply_to_message。",
    {"type": "object", "properties": {
        "receive_id": {"type": "string"},
        "content": {"type": "string", "description": "text 时传正文；post/interactive/image 时传 JSON 字符串"},
        "msg_type": {"type": "string", "enum": ["text", "post", "interactive", "image"]},
        "receive_id_type": {"type": "string", "enum": ["chat_id", "open_id"]},
    }, "required": ["receive_id", "content"]},
)
async def send_feishu_message(input: dict) -> dict[str, Any]:
    try:
        from core.connectors.feishu import FeishuClient
        client = FeishuClient()
        result = client.send_message(
            receive_id=input["receive_id"],
            content=input["content"],
            receive_id_type=input.get("receive_id_type", "chat_id"),
            msg_type=input.get("msg_type", "text"),
        )
        if result is None:
            return {"success": False, "delivery_type": "send", "error": "Failed to send message"}
        return {"success": True, "delivery_type": "send", **result}
    except Exception as e:
        return {"success": False, "delivery_type": "send", "error": str(e)}


@tool(
    "reply_to_message",
    "回复指定的飞书消息。设置 reply_in_thread=true 会创建话题（首次）或在现有话题内回复。返回 message_id 和 thread_id。",
    {"type": "object", "properties": {
        "message_id": {"type": "string", "description": "要回复的飞书消息 ID（platform_message_id）"},
        "content": {"type": "string", "description": "text 时传正文；post/interactive/image 时传 JSON 字符串"},
        "msg_type": {"type": "string", "enum": ["text", "post", "interactive", "image"]},
        "reply_in_thread": {"type": "boolean", "description": "是否在话题内回复（默认 true）"},
        "db_session_id": {"type": "integer", "description": "DB 会话 ID（可选，用于自动绑定 thread_id 到 session）"},
    }, "required": ["message_id", "content"]},
)
async def reply_to_message(input: dict) -> dict[str, Any]:
    try:
        from core.connectors.feishu import FeishuClient
        client = FeishuClient()
        reply_in_thread = input.get("reply_in_thread", True)
        result = client.reply_message(
            message_id=input["message_id"],
            content=input["content"],
            msg_type=input.get("msg_type", "text"),
            reply_in_thread=reply_in_thread,
        )
        if result is None:
            return {"success": False, "delivery_type": "reply", "error": "Failed to reply message"}

        # Auto-bind thread_id to session if provided
        thread_id = result.get("thread_id", "")
        db_session_id = input.get("db_session_id")
        if thread_id and db_session_id:
            try:
                import aiosqlite
                db_path = PROJECT_ROOT / "data" / "db" / "app.sqlite"
                async with aiosqlite.connect(str(db_path)) as db:
                    # Only set if session doesn't already have a thread_id
                    cursor = await db.execute(
                        "SELECT thread_id FROM sessions WHERE id = ?", (db_session_id,)
                    )
                    row = await cursor.fetchone()
                    if row and not row[0]:
                        from datetime import datetime
                        await db.execute(
                            "UPDATE sessions SET thread_id = ?, updated_at = ? WHERE id = ?",
                            (thread_id, datetime.now().isoformat(), db_session_id),
                        )
                        await db.commit()
                        logger.info("Bound thread_id {} to session {}", thread_id, db_session_id)
            except Exception as e:
                logger.warning("Failed to bind thread_id to session: {}", e)

        return {"success": True, "delivery_type": "reply", **result}
    except Exception as e:
        return {"success": False, "delivery_type": "reply", "error": str(e)}


@tool(
    "save_bot_reply",
    "保存机器人回复到数据库（在 send_feishu_message 之后调用，确保回复可追溯）。",
    {"type": "object", "properties": {
        "message_id": {"type": "integer", "description": "原始消息的 DB ID"},
        "chat_id": {"type": "string", "description": "飞书 chat_id"},
        "reply_content": {"type": "string", "description": "回复内容"},
        "session_id": {"type": "integer", "description": "会话 ID（可选）"},
        "is_draft": {"type": "boolean", "description": "是否为草稿"},
    }, "required": ["message_id", "chat_id", "reply_content"]},
)
async def save_bot_reply(input: dict) -> dict[str, Any]:
    """Save a bot reply to the messages table so it's trackable in admin UI."""
    import aiosqlite
    from datetime import datetime, UTC
    db_path = PROJECT_ROOT / "data" / "db" / "app.sqlite"
    try:
        async with aiosqlite.connect(str(db_path)) as db:
            # Look up the original message's platform_message_id
            cursor = await db.execute(
                "SELECT platform_message_id, platform FROM messages WHERE id = ?",
                (input["message_id"],),
            )
            row = await cursor.fetchone()
            if not row:
                return {"success": False, "error": f"message {input['message_id']} not found"}
            platform_msg_id = row[0]
            platform = row[1]

            now = datetime.now().isoformat()
            prefix = "[草稿] " if input.get("is_draft") else ""
            content = f"{prefix}{input['reply_content']}"
            reply_platform_id = f"reply_{platform_msg_id}"
            session_id = input.get("session_id")

            # Upsert — avoid duplicate replies
            cursor = await db.execute(
                "SELECT id FROM messages WHERE platform_message_id = ?",
                (reply_platform_id,),
            )
            existing = await cursor.fetchone()
            if existing:
                await db.execute(
                    "UPDATE messages SET content = ?, processed_at = ? WHERE id = ?",
                    (content, now, existing[0]),
                )
            else:
                await db.execute(
                    "INSERT INTO messages (platform, platform_message_id, chat_id, sender_id, "
                    "sender_name, message_type, content, received_at, classified_type, session_id, "
                    "pipeline_status, processed_at, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (platform, reply_platform_id, input["chat_id"], "bot", "WorkAgent",
                     "text", content, now, "bot_reply", session_id,
                     "completed", now, now),
                )
                # Link to session if provided
                if session_id:
                    cursor2 = await db.execute(
                        "SELECT COALESCE(MAX(sequence_no), 0) FROM session_messages WHERE session_id = ?",
                        (session_id,),
                    )
                    max_seq = (await cursor2.fetchone())[0]
                    reply_id = (await (await db.execute("SELECT last_insert_rowid()")).fetchone())[0]
                    await db.execute(
                        "INSERT INTO session_messages (session_id, message_id, role, sequence_no, created_at) "
                        "VALUES (?,?,?,?,?)",
                        (session_id, reply_id, "assistant", max_seq + 1, now),
                    )
            if session_id:
                await db.execute(
                    "UPDATE sessions SET last_active_at = ?, updated_at = ? WHERE id = ?",
                    (now, now, session_id),
                )
            await db.commit()
            return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}


@tool(
    "read_memory",
    "读取 data/memory 下的记忆文件（项目知识、个人偏好等）。",
    {"type": "object", "properties": {"path": {"type": "string", "description": "相对于 data/memory/ 的路径"}}, "required": ["path"]},
)
async def read_memory(input: dict) -> dict[str, Any]:
    fp = PROJECT_ROOT / "data" / "memory" / input["path"]
    if not fp.exists():
        return {"error": f"文件不存在: {input['path']}"}
    try:
        return {"path": input["path"], "content": fp.read_text(encoding="utf-8")}
    except Exception as e:
        return {"error": str(e)}


@tool(
    "write_memory",
    "写入 data/memory 下的记忆文件。",
    {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]},
)
async def write_memory(input: dict) -> dict[str, Any]:
    fp = PROJECT_ROOT / "data" / "memory" / input["path"]
    try:
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(input["content"], encoding="utf-8")
        return {"success": True, "path": input["path"]}
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
    "upsert_memory_entry",
    "创建或更新结构化长期记忆。适合沉淀项目决策、里程碑、问题及解法、个人偏好、人物信息。",
    {"type": "object", "properties": {
        "entry_id": {"type": "integer", "description": "存在则更新该记忆，不传则创建"},
        "scope": {"type": "string", "enum": ["project", "personal", "people", "general"]},
        "project_name": {"type": "string"},
        "project_version": {"type": "string", "description": "项目版本号，如 1.0、3.0、v4.8.0.6"},
        "project_branch": {"type": "string", "description": "Git 分支名"},
        "project_commit_sha": {"type": "string", "description": "Git commit 短 SHA"},
        "project_commit_time": {"type": "string", "description": "Git commit 时间，ISO 日期时间"},
        "category": {"type": "string", "enum": ["decision", "milestone", "issue", "solution", "preference", "person", "fact", "note"]},
        "title": {"type": "string"},
        "content": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "importance": {"type": "integer", "description": "1-5"},
        "happened_at": {"type": "string", "description": "ISO 日期或日期时间"},
        "valid_until": {"type": "string", "description": "ISO 日期或日期时间"},
        "source_type": {"type": "string"},
        "source_session_id": {"type": "integer"},
        "source_message_id": {"type": "integer"},
        "occurrence_count": {"type": "integer"},
    }, "required": ["title", "content"]},
)
async def upsert_memory_entry(input: dict) -> dict[str, Any]:
    from core.database import async_session_factory
    from core.memory.store import (
        create_memory_entry,
        get_memory_entry,
        memory_entry_to_dict,
        update_memory_entry as _update_memory_entry,
    )

    try:
        async with async_session_factory() as db:
            entry_id = input.get("entry_id")
            if entry_id:
                existing = await get_memory_entry(db, int(entry_id))
                if not existing:
                    return {"error": f"memory entry {entry_id} not found"}
                updated = await _update_memory_entry(db, existing, input)
                return {"action": "updated", "entry": memory_entry_to_dict(updated)}

            created = await create_memory_entry(db, input)
            return {"action": "created", "entry": memory_entry_to_dict(created)}
    except Exception as e:
        return {"error": str(e)}


@tool(
    "update_session",
    "更新工作会话的状态、标题等字段。",
    {"type": "object", "properties": {"session_id": {"type": "integer"}, "updates": {"type": "object"}}, "required": ["session_id", "updates"]},
)
async def update_session(input: dict) -> dict[str, Any]:
    import aiosqlite
    from datetime import datetime, UTC
    allowed = {"title", "topic", "project", "priority", "status", "risk_level", "needs_manual_review"}
    updates = {k: v for k, v in input["updates"].items() if k in allowed}
    if not updates:
        return {"error": "没有有效的更新字段"}
    updates["updated_at"] = datetime.now().isoformat()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [input["session_id"]]
    db_path = PROJECT_ROOT / "data" / "db" / "app.sqlite"
    try:
        async with aiosqlite.connect(str(db_path)) as db:
            await db.execute(f"UPDATE sessions SET {set_clause} WHERE id = ?", values)
            await db.commit()
            return {"success": True, "updated_fields": list(updates.keys())}
    except Exception as e:
        return {"error": str(e)}



@tool(
    "link_task_context",
    "将会话关联到任务上下文。查找匹配的已有任务或创建新任务。返回 task_context_id 和 title。",
    {"type": "object", "properties": {
        "session_id": {"type": "integer", "description": "会话 ID"},
        "title": {"type": "string", "description": "会话标题"},
        "topic": {"type": "string", "description": "会话主题"},
        "project": {"type": "string", "description": "涉及的项目"},
        "match_task_id": {"type": "integer", "description": "匹配到的已有任务 ID（如果你判断应该归入已有任务）"},
        "new_title": {"type": "string", "description": "新任务标题（如果需要创建新任务）"},
    }, "required": ["session_id"]},
)
async def link_task_context(input: dict) -> dict[str, Any]:
    """Link a session to a task context (existing or new)."""
    import aiosqlite
    from datetime import datetime, UTC

    db_path = PROJECT_ROOT / "data" / "db" / "app.sqlite"
    session_id = input["session_id"]
    match_id = input.get("match_task_id")

    try:
        async with aiosqlite.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row

            # If agent specifies a match, verify and link
            if match_id:
                cursor = await db.execute("SELECT id, title FROM task_contexts WHERE id = ?", (match_id,))
                row = await cursor.fetchone()
                if row:
                    await db.execute(
                        "UPDATE sessions SET task_context_id = ?, updated_at = ? WHERE id = ?",
                        (match_id, datetime.now().isoformat(), session_id),
                    )
                    await db.execute(
                        "UPDATE task_contexts SET updated_at = ? WHERE id = ?",
                        (datetime.now().isoformat(), match_id),
                    )
                    await db.commit()
                    return {"task_context_id": match_id, "title": row["title"], "action": "linked_existing"}

            # Create new task context
            now = datetime.now()
            title = input.get("new_title") or input.get("title") or input.get("topic") or "未命名任务"
            await db.execute(
                "INSERT INTO task_contexts (title, description, status, created_at, updated_at) VALUES (?,?,?,?,?)",
                (title, "", "active", now.isoformat(), now.isoformat()),
            )
            await db.commit()
            cursor = await db.execute("SELECT last_insert_rowid()")
            tc_id = (await cursor.fetchone())[0]

            await db.execute(
                "UPDATE sessions SET task_context_id = ?, updated_at = ? WHERE id = ?",
                (tc_id, now.isoformat(), session_id),
            )
            await db.commit()
            return {"task_context_id": tc_id, "title": title, "action": "created_new"}

    except Exception as e:
        return {"error": str(e)}



# ==================== Project Tools ====================

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
    "dispatch_to_project",
    "将任务派发到指定项目的 Agent，在该项目目录下执行，使用该项目的 skills。"
    "如果提供 session_id（会话上下文中的 agent_session_id），则恢复之前的对话上下文，实现多轮交互。"
    "返回项目 Agent 的输出结果和 session_id（下次 resume 用）。",
    {"type": "object", "properties": {
        "project_name": {"type": "string", "description": "projects.yaml 中注册的项目名称"},
        "task": {"type": "string", "description": "要派发给项目 Agent 的任务描述（详细）"},
        "context": {"type": "string", "description": "消息的原始内容和背景信息，供项目 Agent 参考"},
        "session_id": {"type": "string", "description": "上次 dispatch 返回的 session_id，传入则恢复上下文（多轮对话）"},
        "db_session_id": {"type": "integer", "description": "DB 会话 ID，用于持久化 agent_session_id"},
    }, "required": ["project_name", "task"]},
)
async def dispatch_to_project(input: dict) -> dict[str, Any]:
    from core.projects import get_project, merge_skills
    from skills import SKILL_REGISTRY

    runtime = agent_client.get_active_runtime()
    project_name = input["project_name"]
    task = input["task"]
    context = input.get("context", "")
    resume_session_id = input.get("session_id")  # SDK session ID for resume
    db_session_id = input.get("db_session_id")

    project = get_project(project_name)
    if not project:
        return {"error": f"Project '{project_name}' not found. Call list_projects to see available projects."}

    # Track dispatch as a separate AgentRun
    import aiosqlite
    from datetime import datetime
    db_path = PROJECT_ROOT / "data" / "db" / "app.sqlite"
    dispatch_run_id = None
    try:
        async with aiosqlite.connect(str(db_path)) as db:
            now = datetime.now().isoformat()
            cursor = await db.execute(
                "INSERT INTO agent_runs (agent_name, runtime_type, session_id, status, "
                "input_path, output_path, input_tokens, output_tokens, cost_usd, error_message, started_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (f"dispatch:{project_name}", get_agent_run_runtime_type(runtime), db_session_id, "running",
                 "", "", 0, 0, 0.0, "", now),
            )
            dispatch_run_id = cursor.lastrowid
            await db.commit()
    except Exception as e:
        logger.warning("Failed to create dispatch AgentRun: {}", e)

    # Merge skills: project overrides global
    merged_agents = merge_skills(SKILL_REGISTRY, project.path, include_global=False)

    # List project-specific skills for the prompt
    project_only = list(merged_agents)
    skills_line = ""
    if project_only:
        skills_line = f"\n\n可用的项目 Skills: {', '.join(project_only)}"

    # Build prompts for the project sub-agent
    project_prompt = f"""你是项目 "{project.name}" 的工作 Agent。

项目目录: {project.path}
项目说明: {project.description}{skills_line}

## 回复规则
{PROJECT_AGENT_RESPONSE_RULES}

## 任务
{task}"""

    if context:
        project_prompt += f"\n\n## 消息背景\n{context}"

    project_prompt += (
        "\n\n请在项目上下文中处理以上任务，可以使用 Read、Bash、Glob、Grep 等工具访问项目文件。"
        "如果需要回顾项目历史决策、里程碑、已知问题或个人偏好，优先用 search_memory_entries 检索结构化记忆；"
        "当你确认本轮产生了长期有效的项目信息或偏好信息时，可用 upsert_memory_entry 进行沉淀。"
    )

    project_system = (
        f"你运行在项目 {project.name} 的工作目录中（{project.path}）。使用项目级 skills 完成任务。"
        "优先复用结构化长期记忆，不要重复发明已经记录的项目结论。"
        f"\n\n{PROJECT_AGENT_RESPONSE_RULES}"
    )

    try:
        result = await agent_client.run_for_project(
            prompt=project_prompt,
            system_prompt=project_system,
            project_cwd=str(project.path),
            project_agents=merged_agents,
            max_turns=20,
            session_id=resume_session_id,
            runtime=runtime,
        )

        new_session_id = result.get("session_id")

        # Update dispatch AgentRun with result
        if dispatch_run_id:
            try:
                async with aiosqlite.connect(str(db_path)) as db:
                    now = datetime.now().isoformat()
                    await db.execute(
                        "UPDATE agent_runs SET status=?, ended_at=?, cost_usd=?, output_path=? WHERE id=?",
                        ("success", now, result.get("cost_usd", 0),
                         result.get("text", "")[:2000], dispatch_run_id),
                    )
                    await db.commit()
            except Exception as e:
                logger.warning("Failed to update dispatch AgentRun: {}", e)

        # Persist the SDK session_id and project to DB session for future resume
        if db_session_id:
            try:
                async with aiosqlite.connect(str(db_path)) as db:
                    updates = ["project = ?", "agent_runtime = ?", "updated_at = ?"]
                    params = [project_name, runtime, datetime.now().isoformat()]
                    if new_session_id:
                        updates.append("agent_session_id = ?")
                        params.append(new_session_id)
                    params.append(db_session_id)
                    await db.execute(
                        f"UPDATE sessions SET {', '.join(updates)} WHERE id = ?",
                        params,
                    )
                    await db.commit()
            except Exception as e:
                logger.warning("Failed to update session after dispatch: {}", e)

        return {
            "project": project_name,
            "result": result.get("text", ""),
            "session_id": new_session_id,
        }
    except Exception as e:
        logger.exception("dispatch_to_project failed for {}: {}", project_name, e)
        # Mark dispatch AgentRun as failed
        if dispatch_run_id:
            try:
                async with aiosqlite.connect(str(db_path)) as db:
                    now = datetime.now().isoformat()
                    await db.execute(
                        "UPDATE agent_runs SET status=?, ended_at=?, error_message=? WHERE id=?",
                        ("failed", now, str(e)[:500], dispatch_run_id),
                    )
                    await db.commit()
            except Exception:
                pass
        return {"error": str(e), "project": project_name}


# ==================== MCP Server ====================

# All tools registered in MCP (project agents get full set)
CUSTOM_TOOLS = [query_db, write_audit_log, send_feishu_message, reply_to_message,
                save_bot_reply,
                read_memory, write_memory, search_memory_entries, upsert_memory_entry,
                update_session, link_task_context,
                list_projects_tool, dispatch_to_project]

PROJECT_TOOLS = [
    query_db,
    write_audit_log,
    read_memory,
    write_memory,
    search_memory_entries,
    upsert_memory_entry,
    update_session,
    link_task_context,
]

CUSTOM_MCP_SERVER = create_sdk_mcp_server(
    name="work-agent-tools",
    version="1.0.0",
    tools=CUSTOM_TOOLS,
)

CUSTOM_TOOL_NAMES = [f"mcp__work-agent-tools__{t.name}" for t in CUSTOM_TOOLS]
PROJECT_TOOL_NAMES = [f"mcp__work-agent-tools__{t.name}" for t in PROJECT_TOOLS]

PROJECT_AGENT_RESPONSE_RULES = """
处理原则：
1. 默认先尽可能搜索代码、配置、脚本、注释、日志线索和结构化记忆，再给用户结论。
2. 只有在你已经搜索过仍然缺少“会直接影响结论”的关键上下文时，才允许向用户追问。
3. 在没有至少完成一次仓库内检索（Read/Grep/Glob/Bash/结构化记忆检索）之前，不允许直接向用户追问。
4. 如果必须追问，先简短说明你已经检查过什么，再只问最少必要的问题，通常 1 个，而且必须先给出已确认的部分结论。
5. 如果问题存在多层实现或多个可能口径，优先先给出你已经能确认的部分结论，再说明还缺哪一个关键信息。
6. 对工作类问题，优先输出一个“纯 JSON 对象”作为结构化回复，且不要包在代码块里。普通说明/分析优先用 `format=rich`；流程、步骤、链路、时序优先用 `format=flow`。
7. `format=rich` 示例：
   {"format":"rich","title":"标题","summary":"一句话总结","sections":[{"title":"结论","content":"核心结论"},{"title":"说明","content":"补充说明"}],"table":{"columns":[{"key":"item","label":"检查项","type":"text"}],"rows":[{"item":"配置"}]},"fallback_text":"纯文本兜底"}
8. `format=flow` 示例：
   {"format":"flow","title":"标题","summary":"一句话说明","steps":[{"title":"步骤1","detail":"说明"}],"table":{"columns":[{"key":"step","label":"步骤","type":"text"}],"rows":[{"step":"准备"}]},"mermaid":"flowchart TD\\nA[开始]-->B[结束]","fallback_text":"纯文本兜底"}
9. 闲聊、问候、极短确认可以继续输出自然语言正文。
10. 不要输出 action/classified_type/topic/project_name/reply_content 等外层元字段。
""".strip()

# Orchestrator: separate MCP server with limited tools only.
# allowedTools does not restrict MCP tools, so we must register a
# dedicated server that only exposes the tools the orchestrator needs.
ORCHESTRATOR_TOOLS = [query_db, read_memory, search_memory_entries, dispatch_to_project]

ORCHESTRATOR_MCP_SERVER = create_sdk_mcp_server(
    name="work-agent-tools",
    version="1.0.0",
    tools=ORCHESTRATOR_TOOLS,
)

ORCHESTRATOR_TOOL_NAMES = [
    f"mcp__work-agent-tools__{t.name}" for t in ORCHESTRATOR_TOOLS
]


# ==================== Subagent Transcript Hook ====================

async def _on_subagent_stop(
    hook_input: SubagentStopHookInput,
    _progress: str | None,
    _context: HookContext,
) -> dict:
    """Capture sub-agent transcripts when they complete."""
    import aiosqlite
    from datetime import datetime, UTC
    from pathlib import Path

    agent_id = hook_input.agent_id
    agent_type = hook_input.agent_type
    transcript_path = hook_input.agent_transcript_path

    logger.info("SubagentStop: agent={} type={} transcript={}",
                agent_id, agent_type, transcript_path)

    # Read transcript if available
    transcript_content = ""
    if transcript_path:
        try:
            tp = Path(transcript_path)
            if tp.exists():
                transcript_content = tp.read_text(encoding="utf-8")[:10000]
        except Exception as e:
            logger.warning("Failed to read transcript {}: {}", transcript_path, e)

    # Save to agent_runs table
    db_path = PROJECT_ROOT / "data" / "db" / "app.sqlite"
    try:
        async with aiosqlite.connect(str(db_path)) as db:
            now = datetime.now().isoformat()
            await db.execute(
                "INSERT INTO agent_runs (agent_name, runtime_type, input_path, output_path, "
                "status, started_at, ended_at) VALUES (?,?,?,?,?,?,?)",
                (f"subagent:{agent_type}", get_agent_run_runtime_type("claude"), transcript_path or "",
                 transcript_content[:2000], "success", now, now),
            )
            await db.commit()
    except Exception as e:
        logger.warning("Failed to save subagent run: {}", e)

    return {"continue_": True}


# ==================== Agent Client ====================

class AgentClient:
    """Unified agent runtime client with skills, session management, and custom tools."""

    def get_active_runtime(self) -> str:
        return _ACTIVE_AGENT_RUNTIME.get()

    def _resolve_runtime(self, runtime: str | None = None) -> str:
        return normalize_agent_runtime(
            runtime or get_agent_runtime_override() or settings.default_agent_runtime
        )

    def _select_model(self, model: str | None = None) -> str | None:
        runtime = self.get_active_runtime()
        config = load_models_config()
        from core.config import get_default_model_for_runtime

        return (
            model
            or get_model_override(runtime)
            or config.get("default")
            or get_default_model_for_runtime(config, runtime)
        )

    def _get_env(self) -> dict[str, str]:
        return {
            "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
                or os.environ.get("ANTHROPIC_API_KEY", "")
                or settings.anthropic_auth_token
                or settings.anthropic_api_key,
            "ANTHROPIC_BASE_URL": os.environ.get("ANTHROPIC_BASE_URL", "")
                or settings.anthropic_base_url,
        }

    def _get_codex_env(self) -> dict[str, str]:
        env: dict[str, str] = {}
        openai_key = os.environ.get("OPENAI_API_KEY", "") or settings.openai_api_key
        openai_base_url = os.environ.get("OPENAI_BASE_URL", "") or settings.openai_base_url
        if openai_key:
            env["OPENAI_API_KEY"] = openai_key
        if openai_base_url:
            env["OPENAI_BASE_URL"] = openai_base_url
        return env

    async def _run_cli_resume(
        self,
        prompt: str,
        session_id: str,
        cwd: str | None = None,
        model: str | None = None,
        max_turns: int = 20,
        system_prompt: str | None = None,
    ) -> dict[str, Any]:
        """Resume session via CLI subprocess directly.

        Bypasses SDK's --input-format stream-json which has a bug
        causing exit code 1 when combined with --resume.
        """
        cli_path = shutil.which("claude")
        if not cli_path:
            raise RuntimeError("claude CLI not found in PATH")

        cmd = [
            cli_path,
            "--resume", session_id,
            "-p", prompt,
            "--output-format", "json",
            "--max-turns", str(max_turns),
            "--permission-mode", "bypassPermissions",
        ]
        if model:
            cmd.extend(["--model", model])
        if system_prompt:
            cmd.extend(["--system-prompt", system_prompt])

        env = {**os.environ, **self._get_env()}
        env.pop("CLAUDECODE", None)

        logger.info("CLI resume: session={}, cwd={}, model={}, prompt={}",
                     session_id, cwd or "(default)", model, prompt[:100])

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
        )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=300,
            )
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError(f"CLI resume timed out (session={session_id})")

        stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()

        if proc.returncode != 0:
            raise RuntimeError(
                f"CLI resume exit code {proc.returncode} (session={session_id})\n"
                f"Stderr: {stderr_text}"
            )

        output = stdout_bytes.decode("utf-8", errors="replace").strip()
        if not output:
            return {"text": "", "session_id": session_id, "is_error": True}

        # Parse last JSON object containing "result" key
        data = None
        for line in reversed(output.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                candidate = json.loads(line)
                if isinstance(candidate, dict) and "result" in candidate:
                    data = candidate
                    break
            except json.JSONDecodeError:
                continue

        if not data:
            return {"text": output, "session_id": session_id}

        return {
            "text": data.get("result", ""),
            "session_id": data.get("session_id", session_id),
            "duration_ms": data.get("duration_ms"),
            "num_turns": data.get("num_turns"),
            "cost_usd": data.get("total_cost_usd", 0),
            "is_error": data.get("is_error", False),
            "usage": data.get("usage", {}),
            "model": model,
        }

    def _render_skill_block(
        self,
        skill: str | None = None,
        project_agents: dict[str, AgentDefinition] | None = None,
    ) -> str:
        from skills import SKILL_REGISTRY

        blocks: list[str] = []

        if skill:
            definition = (project_agents or {}).get(skill) or SKILL_REGISTRY.get(skill)
            if definition:
                blocks.append(
                    "\n".join(
                        part
                        for part in [
                            f"### 指定 Skill: {skill}",
                            definition.description,
                            definition.prompt,
                        ]
                        if part
                    )
                )
            else:
                blocks.append(f"### 指定 Skill: {skill}\n优先按 {skill} 的职责完成任务。")

        if project_agents:
            for name, definition in project_agents.items():
                if skill and name == skill:
                    continue
                blocks.append(
                    "\n".join(
                        part
                        for part in [
                            f"### 可用 Skill: {name}",
                            definition.description,
                            definition.prompt,
                        ]
                        if part
                    )
                )

        text = "\n\n".join(blocks).strip()
        if len(text) > 6000:
            return text[:6000] + "\n\n[技能说明已截断]"
        return text

    def _build_codex_prompt(
        self,
        prompt: str,
        *,
        system_prompt: str = "",
        skill: str | None = None,
        project_agents: dict[str, AgentDefinition] | None = None,
        orchestrator_mode: bool = False,
    ) -> str:
        sections: list[str] = []
        if system_prompt:
            sections.append(f"## Operating Context\n{system_prompt}")

        skill_block = self._render_skill_block(skill=skill, project_agents=project_agents)
        if skill_block:
            sections.append(f"## Skill Context\n{skill_block}")

        if orchestrator_mode:
            sections.append(
                "## Runtime Notes\n"
                "你运行在 Codex CLI 中，仓库内置本地 MCP 工具。"
                "涉及项目路由、数据库读取或记忆读取时优先使用 MCP 工具。"
            )

        sections.append(f"## User Task\n{prompt}")
        return "\n\n".join(sections)

    def _toml_string(self, value: str) -> str:
        return json.dumps(value)

    def _toml_string_array(self, values: list[str]) -> str:
        return "[" + ", ".join(self._toml_string(v) for v in values) + "]"

    def _build_codex_mcp_overrides(self, scope: str) -> list[str]:
        server_path = PROJECT_ROOT / "core" / "orchestrator" / "codex_mcp_server.py"
        return [
            "-c", f"mcp_servers.work_agent.command={self._toml_string(sys.executable)}",
            "-c", (
                "mcp_servers.work_agent.args="
                f"{self._toml_string_array([str(server_path), '--scope', scope])}"
            ),
        ]

    def _build_codex_command(
        self,
        *,
        session_id: str | None = None,
        cwd: str | None = None,
        model: str | None = None,
        scope: str = "orchestrator",
    ) -> list[str]:
        cli_path = str(_NPM_CODEX_EXE) if _NPM_CODEX_EXE.exists() else (shutil.which("codex") or "")
        if not cli_path:
            raise RuntimeError("codex CLI not found in PATH")

        run_cwd = cwd or str(PROJECT_ROOT)
        cmd = [
            cli_path,
            "exec",
            "--json",
            "--skip-git-repo-check",
            "--full-auto",
            "-C",
            run_cwd,
        ]
        if Path(run_cwd).resolve() != PROJECT_ROOT.resolve():
            cmd.extend(["--add-dir", str(PROJECT_ROOT)])
        if model:
            cmd.extend(["-m", model])
        cmd.extend(self._build_codex_mcp_overrides(scope))
        if session_id:
            cmd.extend(["resume", session_id, "-"])
        else:
            cmd.append("-")
        return cmd

    def _extract_codex_usage(self, payload: dict[str, Any]) -> dict[str, Any]:
        info = payload.get("info") or {}
        total = info.get("total_token_usage") or info.get("last_token_usage") or {}
        return {
            "input_tokens": total.get("input_tokens", 0),
            "cache_read_input_tokens": total.get("cached_input_tokens", 0),
            "output_tokens": total.get("output_tokens", 0),
            "reasoning_output_tokens": total.get("reasoning_output_tokens", 0),
        }

    def _parse_codex_output(
        self,
        output: str,
        *,
        session_id: str | None,
        model: str | None,
    ) -> dict[str, Any]:
        state: dict[str, Any] = {
            "text": "",
            "session_id": session_id,
            "duration_ms": None,
            "num_turns": None,
            "cost_usd": 0,
            "is_error": False,
            "usage": {},
            "model": model,
        }

        for raw_line in output.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            outer_type = event.get("type")
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else event
            event_type = payload.get("type") if outer_type in {"event_msg", "response_item"} else outer_type

            if event_type == "thread.started":
                state["session_id"] = event.get("thread_id") or state["session_id"]
            elif event_type == "agent_message":
                message = payload.get("message") or event.get("message") or ""
                if message:
                    state["text"] = message
            elif event_type == "item.completed":
                item = event.get("item") if isinstance(event.get("item"), dict) else payload.get("item", {})
                if item.get("type") == "agent_message" and item.get("text"):
                    state["text"] = item["text"]
            elif event_type == "agent_message_delta":
                delta = payload.get("delta") or event.get("delta") or ""
                if delta:
                    state["text"] += delta
            elif event_type == "task_complete" and not state["text"]:
                state["text"] = payload.get("last_agent_message", "")
            elif event_type == "message":
                if payload.get("role") == "assistant":
                    message_text = self._extract_message_text(payload)
                    if message_text:
                        state["text"] = message_text
            elif event_type == "token_count":
                state["usage"] = self._extract_codex_usage(payload)
            elif event_type == "turn.completed":
                usage = event.get("usage") if isinstance(event.get("usage"), dict) else payload.get("usage", {})
                if usage:
                    state["usage"] = {
                        "input_tokens": usage.get("input_tokens", 0),
                        "cache_read_input_tokens": usage.get("cached_input_tokens", 0),
                        "output_tokens": usage.get("output_tokens", 0),
                        "reasoning_output_tokens": usage.get("reasoning_output_tokens", 0),
                    }
            elif event_type == "error":
                state["last_error"] = payload.get("message") or event.get("message") or ""
            elif event_type == "turn.failed":
                state["is_error"] = True
                error = payload.get("error") or {}
                state["text"] = state["text"] or error.get("message", "")

        if state.get("last_error") and not state["text"]:
            state["text"] = state["last_error"]
            state["is_error"] = True

        return state

    async def _run_codex(
        self,
        prompt: str,
        *,
        system_prompt: str = "",
        max_turns: int = 30,
        session_id: str | None = None,
        skill: str | None = None,
        model: str | None = None,
        cwd: str | None = None,
        project_agents: dict[str, AgentDefinition] | None = None,
        scope: str = "orchestrator",
        orchestrator_mode: bool = False,
    ) -> dict[str, Any]:
        selected_model = self._select_model(model)
        codex_prompt = self._build_codex_prompt(
            prompt,
            system_prompt=system_prompt,
            skill=skill,
            project_agents=project_agents,
            orchestrator_mode=orchestrator_mode,
        )
        cmd = self._build_codex_command(
            session_id=session_id,
            cwd=cwd,
            model=selected_model,
            scope=scope,
        )
        env = {**os.environ, **self._get_codex_env()}

        logger.info(
            "Codex run: scope={}, cwd={}, model={}, resume={}",
            scope,
            cwd or str(PROJECT_ROOT),
            selected_model,
            bool(session_id),
        )

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd or str(PROJECT_ROOT),
            env=env,
        )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(codex_prompt.encode("utf-8")),
                timeout=max(300, max_turns * 30),
            )
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError(f"codex exec timed out (session={session_id or 'new'})")

        stdout_text = stdout_bytes.decode("utf-8", errors="replace").strip()
        stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
        result = self._parse_codex_output(
            stdout_text,
            session_id=session_id,
            model=selected_model,
        )

        if not result.get("text") and result.get("session_id"):
            _, rollout = self._read_codex_rollout(result["session_id"])
            if rollout:
                rollout_text = "\n".join(json.dumps(item, ensure_ascii=False) for item in rollout)
                fallback = self._parse_codex_output(
                    rollout_text,
                    session_id=result["session_id"],
                    model=selected_model,
                )
                if fallback.get("text"):
                    logger.info("Codex stdout empty, recovered final text from rollout {}", result["session_id"])
                    merged_usage = result.get("usage") or fallback.get("usage") or {}
                    result = {
                        **result,
                        **fallback,
                        "usage": merged_usage,
                    }

        if proc.returncode != 0 and not (result.get("is_error") and result.get("text")):
            detail = stderr_text or stdout_text or f"codex exec exit code {proc.returncode}"
            raise RuntimeError(detail)

        return result

    async def _run_codex_stream(
        self,
        prompt: str,
        *,
        system_prompt: str = "",
        max_turns: int = 30,
        session_id: str | None = None,
        skill: str | None = None,
        model: str | None = None,
        cwd: str | None = None,
        project_agents: dict[str, AgentDefinition] | None = None,
        scope: str = "orchestrator",
        orchestrator_mode: bool = False,
    ) -> AsyncIterator[dict]:
        selected_model = self._select_model(model)
        codex_prompt = self._build_codex_prompt(
            prompt,
            system_prompt=system_prompt,
            skill=skill,
            project_agents=project_agents,
            orchestrator_mode=orchestrator_mode,
        )
        cmd = self._build_codex_command(
            session_id=session_id,
            cwd=cwd,
            model=selected_model,
            scope=scope,
        )
        env = {**os.environ, **self._get_codex_env()}

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd or str(PROJECT_ROOT),
            env=env,
        )

        last_text = ""
        current_session_id = session_id
        usage: dict[str, Any] = {}

        async def _read_stderr() -> str:
            if not proc.stderr:
                return ""
            data = await proc.stderr.read()
            return data.decode("utf-8", errors="replace").strip()

        stderr_task = asyncio.create_task(_read_stderr())

        if not proc.stdout:
            raise RuntimeError("codex exec stdout stream unavailable")

        async def _write_stdin() -> None:
            if not proc.stdin:
                return
            proc.stdin.write(codex_prompt.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()

        stdin_task = asyncio.create_task(_write_stdin())

        try:
            while True:
                raw_line = await proc.stdout.readline()
                if not raw_line:
                    break
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                outer_type = event.get("type")
                payload = event.get("payload") if isinstance(event.get("payload"), dict) else event
                event_type = payload.get("type") if outer_type in {"event_msg", "response_item"} else outer_type

                if event_type == "thread.started":
                    current_session_id = event.get("thread_id") or current_session_id
                elif event_type == "agent_message":
                    text = payload.get("message") or event.get("message") or ""
                    if text:
                        if text.startswith(last_text):
                            delta = text[len(last_text):]
                            if delta:
                                yield {"type": "text", "content": delta}
                        else:
                            yield {"type": "text", "content": text}
                        last_text = text
                elif event_type == "item.completed":
                    item = event.get("item") if isinstance(event.get("item"), dict) else payload.get("item", {})
                    if item.get("type") == "agent_message":
                        text = item.get("text") or ""
                        if text:
                            if text.startswith(last_text):
                                delta = text[len(last_text):]
                                if delta:
                                    yield {"type": "text", "content": delta}
                            else:
                                yield {"type": "text", "content": text}
                            last_text = text
                elif event_type == "agent_message_delta":
                    delta = payload.get("delta") or event.get("delta") or ""
                    if delta:
                        last_text += delta
                        yield {"type": "text", "content": delta}
                elif event_type == "exec.command_begin":
                    yield {
                        "type": "tool_use",
                        "tool": payload.get("command", "shell"),
                        "input": json.dumps(payload, ensure_ascii=False)[:500],
                    }
                elif event_type == "token_count":
                    usage = self._extract_codex_usage(payload)
                elif event_type == "turn.completed":
                    raw_usage = event.get("usage") if isinstance(event.get("usage"), dict) else payload.get("usage", {})
                    if raw_usage:
                        usage = {
                            "input_tokens": raw_usage.get("input_tokens", 0),
                            "cache_read_input_tokens": raw_usage.get("cached_input_tokens", 0),
                            "output_tokens": raw_usage.get("output_tokens", 0),
                            "reasoning_output_tokens": raw_usage.get("reasoning_output_tokens", 0),
                        }
                elif event_type == "error":
                    message = payload.get("message") or event.get("message") or ""
                    if message:
                        yield {"type": "error", "message": message}
                elif event_type == "turn.failed":
                    error = payload.get("error") or {}
                    message = error.get("message", "codex exec failed")
                    yield {"type": "error", "message": message}

            await stdin_task
            returncode = await proc.wait()
            stderr_text = await stderr_task
            if returncode != 0:
                raise RuntimeError(stderr_text or "codex exec failed")

            yield {
                "type": "result",
                "session_id": current_session_id,
                "is_error": False,
                "duration_ms": None,
                "num_turns": None,
                "cost_usd": 0,
                "usage": usage,
            }
        finally:
            if not stdin_task.done():
                stdin_task.cancel()
            if not stderr_task.done():
                stderr_task.cancel()

    def _build_options(
        self,
        system_prompt: str = "",
        max_turns: int = 30,
        session_id: Optional[str] = None,
        skill: Optional[str] = None,
        project_cwd: Optional[str] = None,
        project_agents: Optional[dict] = None,
        exclude_tools: Optional[list[str]] = None,
        model: Optional[str] = None,
        orchestrator_mode: bool = False,
    ) -> ClaudeAgentOptions:
        from skills import SKILL_REGISTRY

        builtin_tools = ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]

        # Orchestrator: limited tools (no message sending)
        # Project: full tools
        custom_tools = ORCHESTRATOR_TOOL_NAMES if orchestrator_mode else CUSTOM_TOOL_NAMES
        allowed = builtin_tools + custom_tools
        if exclude_tools:
            allowed = [t for t in allowed if t not in exclude_tools]

        selected_model = self._select_model(model)

        mcp = ORCHESTRATOR_MCP_SERVER if orchestrator_mode else CUSTOM_MCP_SERVER
        opts = ClaudeAgentOptions(
            system_prompt=system_prompt or None,
            mcp_servers={"work-agent-tools": mcp},
            allowed_tools=allowed,
            permission_mode="bypassPermissions",
            max_turns=max_turns,
            cwd=project_cwd or str(PROJECT_ROOT),
            env=self._get_env(),
            model=selected_model,
            # Register skills as sub-agents. For project runs, an empty dict means
            # "no project-local skills", and must not fall back to unrelated globals.
            agents=project_agents if project_agents is not None else SKILL_REGISTRY,
            # Capture sub-agent transcripts
            hooks={
                "SubagentStop": [HookMatcher(hooks=[_on_subagent_stop])],
            },
        )

        # Resume existing session
        if session_id:
            opts.resume = session_id

        return opts

    async def run(
        self,
        prompt: str,
        system_prompt: str = "",
        max_turns: int = 30,
        session_id: Optional[str] = None,
        skill: Optional[str] = None,
        model: Optional[str] = None,
        runtime: Optional[str] = None,
    ) -> dict[str, Any]:
        """Run agent to completion. Returns {"text": ..., "session_id": ...}."""
        resolved_runtime = self._resolve_runtime(runtime)
        token = _ACTIVE_AGENT_RUNTIME.set(resolved_runtime)

        if resolved_runtime == "codex":
            try:
                return await self._run_codex(
                    prompt=prompt,
                    system_prompt=system_prompt,
                    max_turns=max_turns,
                    session_id=session_id,
                    skill=skill,
                    model=model,
                    scope="orchestrator",
                    orchestrator_mode=True,
                )
            finally:
                _ACTIVE_AGENT_RUNTIME.reset(token)

        # If a specific skill is requested, prepend instruction to use it
        try:
            if skill:
                prompt = f"请使用 {skill} agent 来处理以下内容：\n\n{prompt}"

            if session_id:
                selected_model = self._select_model(model)
                return await self._run_cli_resume(
                    prompt=prompt,
                    session_id=session_id,
                    model=selected_model,
                    max_turns=max_turns,
                    system_prompt=system_prompt or None,
                )

            stderr_lines: list[str] = []
            options = self._build_options(
                system_prompt=system_prompt,
                max_turns=max_turns,
                session_id=session_id,
                skill=skill,
                model=model,
                orchestrator_mode=True,
            )
            options.stderr = lambda line: stderr_lines.append(line)

            result_text = ""
            result_session_id = session_id
            result_meta = {}

            try:
                async for msg in query(prompt=prompt, options=options):
                    if isinstance(msg, AssistantMessage) and msg.content:
                        for block in msg.content:
                            if isinstance(block, TextBlock):
                                result_text = block.text
                    elif isinstance(msg, ResultMessage):
                        result_session_id = msg.session_id
                        result_meta = {
                            "duration_ms": msg.duration_ms,
                            "num_turns": msg.num_turns,
                            "cost_usd": msg.total_cost_usd,
                            "is_error": msg.is_error,
                            "usage": msg.usage or {},
                        }
            except Exception as e:
                stderr_text = "\n".join(stderr_lines[-20:])
                if result_meta.get("is_error") and result_text:
                    logger.warning("Agent CLI error (orchestrator): {} (returning result: {})\nStderr:\n{}",
                                   e, result_text[:100], stderr_text)
                else:
                    logger.error("Agent CLI failed (orchestrator): {}\nStderr:\n{}", e, stderr_text)
                    detail = f"{e}\nStderr: {stderr_text}" if stderr_text else str(e)
                    raise RuntimeError(detail) from e

            return {
                "text": result_text,
                "session_id": result_session_id,
                "model": options.model,
                **result_meta,
            }
        finally:
            _ACTIVE_AGENT_RUNTIME.reset(token)

    async def run_stream(
        self,
        prompt: str,
        system_prompt: str = "",
        max_turns: int = 30,
        session_id: Optional[str] = None,
        skill: Optional[str] = None,
        model: Optional[str] = None,
        runtime: Optional[str] = None,
    ) -> AsyncIterator[dict]:
        """Run agent with SSE streaming. Yields event dicts."""
        resolved_runtime = self._resolve_runtime(runtime)
        token = _ACTIVE_AGENT_RUNTIME.set(resolved_runtime)

        try:
            if resolved_runtime == "codex":
                async for event in self._run_codex_stream(
                    prompt=prompt,
                    system_prompt=system_prompt,
                    max_turns=max_turns,
                    session_id=session_id,
                    skill=skill,
                    model=model,
                    scope="orchestrator",
                    orchestrator_mode=True,
                ):
                    yield event
                return

            if skill:
                prompt = f"请使用 {skill} agent 来处理以下内容：\n\n{prompt}"

            options = self._build_options(
                system_prompt=system_prompt,
                max_turns=max_turns,
                session_id=session_id,
                skill=skill,
                model=model,
            )

            async for msg in query(prompt=prompt, options=options):
                if isinstance(msg, SystemMessage):
                    yield {"type": "system", "subtype": msg.subtype}

                elif isinstance(msg, AssistantMessage) and msg.content:
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            yield {"type": "text", "content": block.text}
                        elif isinstance(block, ToolUseBlock):
                            yield {"type": "tool_use", "tool": block.name, "input": json.dumps(block.input, ensure_ascii=False)[:500]}

                elif isinstance(msg, ResultMessage):
                    yield {
                        "type": "result",
                        "session_id": msg.session_id,
                        "is_error": msg.is_error,
                        "duration_ms": msg.duration_ms,
                        "num_turns": msg.num_turns,
                        "cost_usd": msg.total_cost_usd,
                    }
        finally:
            _ACTIVE_AGENT_RUNTIME.reset(token)

    # ---- Project Dispatch ----

    async def run_for_project(
        self,
        prompt: str,
        system_prompt: str,
        project_cwd: str,
        project_agents: dict,
        max_turns: int = 20,
        session_id: Optional[str] = None,
        model: Optional[str] = None,
        runtime: Optional[str] = None,
    ) -> dict[str, Any]:
        """Run an agent in a specific project directory with project-specific skills.

        Args:
            session_id: Pass a previous SDK session_id to resume conversation context.

        Used by dispatch_to_project tool. Excludes recursive dispatch tools.
        """
        resolved_runtime = self._resolve_runtime(runtime)
        token = _ACTIVE_AGENT_RUNTIME.set(resolved_runtime)

        try:
            if resolved_runtime == "codex":
                return await self._run_codex(
                    prompt=prompt,
                    system_prompt=system_prompt,
                    max_turns=max_turns,
                    session_id=session_id,
                    model=model,
                    cwd=project_cwd,
                    project_agents=project_agents,
                    scope="project",
                )

            if session_id:
                selected_model = self._select_model(model)
                return await self._run_cli_resume(
                    prompt=prompt,
                    session_id=session_id,
                    cwd=project_cwd,
                    model=selected_model,
                    max_turns=max_turns,
                    system_prompt=system_prompt,
                )

            stderr_lines: list[str] = []
            options = self._build_options(
                system_prompt=system_prompt,
                max_turns=max_turns,
                session_id=session_id,
                project_cwd=project_cwd,
                project_agents=project_agents,
                exclude_tools=[name for name in CUSTOM_TOOL_NAMES if name not in PROJECT_TOOL_NAMES],
                model=model,
            )
            options.stderr = lambda line: stderr_lines.append(line)

            result_text = ""
            result_session_id = None
            result_meta = {}

            try:
                async for msg in query(prompt=prompt, options=options):
                    if isinstance(msg, AssistantMessage) and msg.content:
                        for block in msg.content:
                            if isinstance(block, TextBlock):
                                result_text = block.text
                    elif isinstance(msg, ResultMessage):
                        result_session_id = msg.session_id
                        result_meta = {
                            "duration_ms": msg.duration_ms,
                            "num_turns": msg.num_turns,
                            "cost_usd": msg.total_cost_usd,
                            "is_error": msg.is_error,
                            "usage": msg.usage or {},
                        }
            except Exception as e:
                stderr_text = "\n".join(stderr_lines[-20:])
                if result_meta.get("is_error") and result_text:
                    logger.warning("Agent CLI error (project={}): {} (returning result: {})\nStderr:\n{}",
                                   project_cwd, e, result_text[:100], stderr_text)
                else:
                    logger.error("Agent CLI failed (project={}): {}\nStderr:\n{}", project_cwd, e, stderr_text)
                    detail = f"{e}\nStderr: {stderr_text}" if stderr_text else str(e)
                    raise RuntimeError(detail) from e

            return {
                "text": result_text,
                "session_id": result_session_id,
                "model": options.model,
                **result_meta,
            }
        finally:
            _ACTIVE_AGENT_RUNTIME.reset(token)

    # ---- Session Management ----

    def _find_codex_session_files(self, session_id: str) -> list[Path]:
        sessions_root = CODEX_HOME / "sessions"
        if not sessions_root.exists():
            return []
        return sorted(
            sessions_root.rglob(f"*{session_id}.jsonl"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )

    def _read_codex_rollout(self, session_id: str) -> tuple[Path | None, list[dict[str, Any]]]:
        files = self._find_codex_session_files(session_id)
        if not files:
            return None, []
        events: list[dict[str, Any]] = []
        for raw_line in files[0].read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return files[0], events

    async def _list_claude_sessions(self) -> list[dict]:
        sessions = await sdk_list_sessions()
        return [
            {
                "id": s.id,
                "created_at": s.created_at,
                "updated_at": s.updated_at,
                "tags": s.tags,
                "runtime": "claude",
            }
            for s in sessions
        ]

    def _list_codex_sessions(self) -> list[dict]:
        from datetime import datetime

        index_path = CODEX_HOME / "session_index.jsonl"
        if not index_path.exists():
            return []

        sessions: list[dict] = []
        for raw_line in index_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            session_id = entry.get("id")
            if not session_id:
                continue

            session_file, rollout = self._read_codex_rollout(session_id)
            created_at = None
            for event in rollout:
                if event.get("type") == "session_meta":
                    payload = event.get("payload") or {}
                    created_at = payload.get("timestamp") or payload.get("created_at")
                    break

            updated_at = entry.get("updated_at")
            if not updated_at and session_file:
                updated_at = datetime.fromtimestamp(session_file.stat().st_mtime).isoformat()

            sessions.append(
                {
                    "id": session_id,
                    "created_at": created_at,
                    "updated_at": updated_at,
                    "tags": [entry.get("thread_name", "")] if entry.get("thread_name") else [],
                    "runtime": "codex",
                }
            )

        return sessions

    def _extract_message_text(self, payload: dict[str, Any]) -> str:
        content = payload.get("content") or []
        texts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") in ("input_text", "output_text"):
                text = item.get("text", "").strip()
                if text:
                    texts.append(text)
        return "\n".join(texts).strip()

    async def list_sessions(self, runtime: Optional[str] = None) -> list[dict]:
        if runtime:
            resolved_runtime = self._resolve_runtime(runtime)
            if resolved_runtime == "claude":
                return await self._list_claude_sessions()
            return self._list_codex_sessions()

        sessions = await self._list_claude_sessions()
        sessions.extend(self._list_codex_sessions())
        sessions.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        return sessions

    async def get_session_messages(
        self,
        session_id: str,
        runtime: Optional[str] = None,
    ) -> list[dict]:
        resolved_runtime = normalize_agent_runtime(runtime or DEFAULT_AGENT_RUNTIME)
        if resolved_runtime == "claude":
            messages = await sdk_get_session_messages(session_id)
            result = []
            for m in messages:
                result.append({"role": m.role, "content": str(m.content)[:1000]})
            return result

        _, rollout = self._read_codex_rollout(session_id)
        messages: list[dict] = []
        for event in rollout:
            if event.get("type") != "response_item":
                continue
            payload = event.get("payload") or {}
            if payload.get("type") != "message":
                continue
            role = payload.get("role")
            if role not in ("user", "assistant"):
                continue
            text = self._extract_message_text(payload)
            if not text or text.startswith("<environment_context>"):
                continue
            messages.append({"role": role, "content": text[:1000]})
        return messages

    async def delete_session(
        self,
        session_id: str,
        runtime: Optional[str] = None,
    ) -> bool:
        resolved_runtime = normalize_agent_runtime(runtime or DEFAULT_AGENT_RUNTIME)
        if resolved_runtime == "claude":
            await sdk_delete_session(session_id)
            return True

        deleted = False
        for path in self._find_codex_session_files(session_id):
            try:
                path.unlink()
                deleted = True
            except OSError:
                continue

        for filename in ("session_index.jsonl", "history.jsonl"):
            target = CODEX_HOME / filename
            if not target.exists():
                continue
            kept: list[str] = []
            changed = False
            for raw_line in target.read_text(encoding="utf-8", errors="replace").splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    kept.append(raw_line)
                    continue
                if item.get("id") == session_id or item.get("session_id") == session_id:
                    changed = True
                    continue
                kept.append(raw_line)
            if changed:
                target.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
                deleted = True

        return deleted


# Singleton
agent_client = AgentClient()
