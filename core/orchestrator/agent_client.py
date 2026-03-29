"""Claude Agent SDK client with skills, custom tools, and session management."""

import json
import os
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
    ToolResultBlock,
)

from core.config import settings

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


# ==================== Custom Tools ====================

@tool(
    "query_db",
    "对本地 SQLite 数据库执行只读 SQL 查询。表: messages, sessions, session_messages, tasks, reports, agent_runs, audit_logs。",
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
    "通过飞书发送消息。",
    {"type": "object", "properties": {"receive_id": {"type": "string"}, "content": {"type": "string"}, "receive_id_type": {"type": "string", "enum": ["chat_id", "open_id"]}}, "required": ["receive_id", "content"]},
)
async def send_feishu_message(input: dict) -> dict[str, Any]:
    try:
        from core.connectors.feishu import FeishuClient
        client = FeishuClient()
        ok = client.send_message(receive_id=input["receive_id"], content=input["content"], receive_id_type=input.get("receive_id_type", "chat_id"))
        return {"success": ok}
    except Exception as e:
        return {"error": str(e)}


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
                return {"error": f"message {input['message_id']} not found"}
            platform_msg_id = row[0]
            platform = row[1]

            now = datetime.now().isoformat()
            prefix = "[草稿] " if input.get("is_draft") else ""
            content = f"{prefix}{input['reply_content']}"
            reply_platform_id = f"reply_{platform_msg_id}"

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
                session_id = input.get("session_id")
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
            await db.commit()
            return {"success": True}
    except Exception as e:
        return {"error": str(e)}


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
    "route_to_session",
    "根据 chat_id 和 topic/project 查找或创建工作会话。返回 session_id。",
    {"type": "object", "properties": {
        "chat_id": {"type": "string", "description": "飞书 chat_id"},
        "sender_id": {"type": "string", "description": "发送者 ID"},
        "message_id": {"type": "integer", "description": "消息 DB ID"},
        "topic": {"type": "string", "description": "消息主题"},
        "project": {"type": "string", "description": "涉及的项目"},
        "priority": {"type": "string", "description": "优先级: high/normal/low"},
        "summary": {"type": "string", "description": "消息摘要"},
    }, "required": ["chat_id", "sender_id", "message_id"]},
)
async def route_to_session(input: dict) -> dict[str, Any]:
    """Find or create a session and attach the message to it."""
    import aiosqlite
    from datetime import datetime, UTC, timedelta

    db_path = PROJECT_ROOT / "data" / "db" / "app.sqlite"
    chat_id = input["chat_id"]
    project = input.get("project", "")
    topic = input.get("topic", "")
    cutoff = (datetime.now() - timedelta(hours=2)).isoformat()

    try:
        async with aiosqlite.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row

            # Strategy 1: same chat_id + project + active within 2h
            if project:
                cursor = await db.execute(
                    "SELECT id, title, topic, project, agent_session_id FROM sessions "
                    "WHERE source_chat_id = ? AND project = ? AND status IN ('open','waiting') "
                    "AND last_active_at >= ? ORDER BY last_active_at DESC LIMIT 1",
                    (chat_id, project, cutoff),
                )
                row = await cursor.fetchone()
                if row:
                    await _attach_msg_to_session(db, row["id"], input["message_id"])
                    return {"session_id": row["id"], "title": row["title"],
                            "agent_session_id": row["agent_session_id"] or None,
                            "action": "matched_by_project"}

            # Strategy 2: same chat_id + active window
            cursor = await db.execute(
                "SELECT id, title, topic, project, agent_session_id FROM sessions "
                "WHERE source_chat_id = ? AND status IN ('open','waiting') "
                "AND last_active_at >= ? ORDER BY last_active_at DESC LIMIT 1",
                (chat_id, cutoff),
            )
            row = await cursor.fetchone()
            if row:
                await _attach_msg_to_session(db, row["id"], input["message_id"])
                return {"session_id": row["id"], "title": row["title"],
                        "agent_session_id": row["agent_session_id"] or None,
                        "action": "matched_by_chat"}

            # Strategy 3: create new session
            now = datetime.now()
            timestamp = now.strftime("%Y%m%d_%H%M%S")
            session_key = f"feishu_{chat_id[:16]}_{timestamp}"
            title = input.get("summary", "")[:128] or topic[:128] or "新会话"

            await db.execute(
                "INSERT INTO sessions (session_key, source_platform, source_chat_id, owner_user_id, "
                "title, topic, project, priority, status, last_active_at, message_count, "
                "risk_level, needs_manual_review, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (session_key, "feishu", chat_id, input["sender_id"],
                 title, topic, project, input.get("priority", "normal"),
                 "open", now.isoformat(), 0, "low", False, now.isoformat(), now.isoformat()),
            )
            await db.commit()
            cursor = await db.execute("SELECT last_insert_rowid()")
            session_id = (await cursor.fetchone())[0]

            await _attach_msg_to_session(db, session_id, input["message_id"])
            return {"session_id": session_id, "title": title, "action": "created_new"}

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


async def _attach_msg_to_session(db, session_id: int, message_id: int) -> None:
    """Attach a message to a session (raw aiosqlite)."""
    from datetime import datetime, UTC
    now = datetime.now().isoformat()

    # Get current message_count
    cursor = await db.execute("SELECT message_count FROM sessions WHERE id = ?", (session_id,))
    row = await cursor.fetchone()
    new_count = (row[0] if row else 0) + 1

    # Update session
    await db.execute(
        "UPDATE sessions SET message_count = ?, last_active_at = ?, updated_at = ? WHERE id = ?",
        (new_count, now, now, session_id),
    )

    # Link message to session
    await db.execute(
        "UPDATE messages SET session_id = ? WHERE id = ?",
        (session_id, message_id),
    )

    # Create session_message record
    await db.execute(
        "INSERT INTO session_messages (session_id, message_id, role, sequence_no, created_at) VALUES (?,?,?,?,?)",
        (session_id, message_id, "user", new_count, now),
    )
    await db.commit()


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
    "如果提供 session_id（从 route_to_session 返回的 agent_session_id），则恢复之前的对话上下文，实现多轮交互。"
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

    project_name = input["project_name"]
    task = input["task"]
    context = input.get("context", "")
    resume_session_id = input.get("session_id")  # SDK session ID for resume
    db_session_id = input.get("db_session_id")

    project = get_project(project_name)
    if not project:
        return {"error": f"Project '{project_name}' not found. Call list_projects to see available projects."}

    # Merge skills: project overrides global
    merged_agents = merge_skills(SKILL_REGISTRY, project.path)

    # List project-specific skills for the prompt
    project_only = [k for k in merged_agents if k not in SKILL_REGISTRY]
    skills_line = ""
    if project_only:
        skills_line = f"\n\n可用的项目 Skills: {', '.join(project_only)}"

    # Build prompts for the project sub-agent
    project_prompt = f"""你是项目 "{project.name}" 的工作 Agent。

项目目录: {project.path}
项目说明: {project.description}{skills_line}

## 任务
{task}"""

    if context:
        project_prompt += f"\n\n## 消息背景\n{context}"

    project_prompt += "\n\n请在项目上下文中处理以上任务，可以使用 Read、Bash、Glob、Grep 等工具访问项目文件。"

    project_system = f"你运行在项目 {project.name} 的工作目录中（{project.path}）。使用项目级 skills 完成任务。"

    try:
        result = await agent_client.run_for_project(
            prompt=project_prompt,
            system_prompt=project_system,
            project_cwd=str(project.path),
            project_agents=merged_agents,
            max_turns=20,
            session_id=resume_session_id,
        )

        new_session_id = result.get("session_id")

        # Persist the SDK session_id to DB session for future resume
        if new_session_id and db_session_id:
            try:
                import aiosqlite
                from datetime import datetime, UTC
                db_path = PROJECT_ROOT / "data" / "db" / "app.sqlite"
                async with aiosqlite.connect(str(db_path)) as db:
                    await db.execute(
                        "UPDATE sessions SET agent_session_id = ?, updated_at = ? WHERE id = ?",
                        (new_session_id, datetime.now().isoformat(), db_session_id),
                    )
                    await db.commit()
            except Exception as e:
                logger.warning("Failed to persist agent_session_id: {}", e)

        return {
            "project": project_name,
            "result": result.get("text", ""),
            "session_id": new_session_id,
        }
    except Exception as e:
        logger.exception("dispatch_to_project failed for {}: {}", project_name, e)
        return {"error": str(e), "project": project_name}


# ==================== MCP Server ====================

CUSTOM_TOOLS = [query_db, write_audit_log, send_feishu_message, save_bot_reply,
                read_memory, write_memory, update_session, route_to_session, link_task_context,
                list_projects_tool, dispatch_to_project]

CUSTOM_MCP_SERVER = create_sdk_mcp_server(
    name="work-agent-tools",
    version="1.0.0",
    tools=CUSTOM_TOOLS,
)

CUSTOM_TOOL_NAMES = [f"mcp__work-agent-tools__{t.name}" for t in CUSTOM_TOOLS]


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
                (f"subagent:{agent_type}", "agent_sdk", transcript_path or "",
                 transcript_content[:2000], "success", now, now),
            )
            await db.commit()
    except Exception as e:
        logger.warning("Failed to save subagent run: {}", e)

    return {"continue_": True}


# ==================== Agent Client ====================

class AgentClient:
    """Agent SDK client with skills, session management, and custom tools."""

    def _get_env(self) -> dict[str, str]:
        return {
            "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
                or os.environ.get("ANTHROPIC_API_KEY", "")
                or settings.anthropic_auth_token
                or settings.anthropic_api_key,
            "ANTHROPIC_BASE_URL": os.environ.get("ANTHROPIC_BASE_URL", "")
                or settings.anthropic_base_url,
        }

    def _build_options(
        self,
        system_prompt: str = "",
        max_turns: int = 30,
        session_id: Optional[str] = None,
        skill: Optional[str] = None,
        project_cwd: Optional[str] = None,
        project_agents: Optional[dict] = None,
        exclude_tools: Optional[list[str]] = None,
    ) -> ClaudeAgentOptions:
        from skills import SKILL_REGISTRY

        builtin_tools = ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]

        allowed = builtin_tools + CUSTOM_TOOL_NAMES
        if exclude_tools:
            allowed = [t for t in allowed if t not in exclude_tools]

        opts = ClaudeAgentOptions(
            system_prompt=system_prompt or None,
            mcp_servers={"work-agent-tools": CUSTOM_MCP_SERVER},
            allowed_tools=allowed,
            permission_mode="bypassPermissions",
            max_turns=max_turns,
            cwd=project_cwd or str(PROJECT_ROOT),
            env=self._get_env(),
            # Register skills as sub-agents (project-specific or global)
            agents=project_agents or SKILL_REGISTRY,
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
    ) -> dict[str, Any]:
        """Run agent to completion. Returns {"text": ..., "session_id": ...}."""
        # If a specific skill is requested, prepend instruction to use it
        if skill:
            prompt = f"请使用 {skill} agent 来处理以下内容：\n\n{prompt}"

        options = self._build_options(
            system_prompt=system_prompt,
            max_turns=max_turns,
            session_id=session_id,
            skill=skill,
        )

        result_text = ""
        result_session_id = session_id

        async for msg in query(prompt=prompt, options=options):
            if isinstance(msg, AssistantMessage) and msg.content:
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        result_text = block.text
            elif isinstance(msg, ResultMessage):
                result_session_id = msg.session_id

        return {"text": result_text, "session_id": result_session_id}

    async def run_stream(
        self,
        prompt: str,
        system_prompt: str = "",
        max_turns: int = 30,
        session_id: Optional[str] = None,
        skill: Optional[str] = None,
    ) -> AsyncIterator[dict]:
        """Run agent with SSE streaming. Yields event dicts."""
        if skill:
            prompt = f"请使用 {skill} agent 来处理以下内容：\n\n{prompt}"

        options = self._build_options(
            system_prompt=system_prompt,
            max_turns=max_turns,
            session_id=session_id,
            skill=skill,
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
                    elif isinstance(block, ToolResultBlock):
                        yield {"type": "tool_result", "content": str(block.content)[:500]}

            elif isinstance(msg, ResultMessage):
                yield {
                    "type": "result",
                    "session_id": msg.session_id,
                    "is_error": msg.is_error,
                    "duration_ms": msg.duration_ms,
                    "num_turns": msg.num_turns,
                    "cost_usd": msg.total_cost_usd,
                }

    # ---- Project Dispatch ----

    async def run_for_project(
        self,
        prompt: str,
        system_prompt: str,
        project_cwd: str,
        project_agents: dict,
        max_turns: int = 20,
        session_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Run an agent in a specific project directory with project-specific skills.

        Args:
            session_id: Pass a previous SDK session_id to resume conversation context.

        Used by dispatch_to_project tool. Excludes recursive dispatch tools.
        """
        # Prevent recursion: project agents cannot dispatch to other projects
        exclude = [
            "mcp__work-agent-tools__dispatch_to_project",
            "mcp__work-agent-tools__list_projects",
        ]

        options = self._build_options(
            system_prompt=system_prompt,
            max_turns=max_turns,
            session_id=session_id,
            project_cwd=project_cwd,
            project_agents=project_agents,
            exclude_tools=exclude,
        )

        result_text = ""
        result_session_id = None

        async for msg in query(prompt=prompt, options=options):
            if isinstance(msg, AssistantMessage) and msg.content:
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        result_text = block.text
            elif isinstance(msg, ResultMessage):
                result_session_id = msg.session_id

        return {"text": result_text, "session_id": result_session_id}

    # ---- Session Management ----

    async def list_sessions(self) -> list[dict]:
        """List all Agent SDK sessions."""
        sessions = await sdk_list_sessions()
        return [{"id": s.id, "created_at": s.created_at, "updated_at": s.updated_at, "tags": s.tags} for s in sessions]

    async def get_session_messages(self, session_id: str) -> list[dict]:
        """Get messages from an Agent SDK session."""
        messages = await sdk_get_session_messages(session_id)
        result = []
        for m in messages:
            result.append({"role": m.role, "content": str(m.content)[:1000]})
        return result

    async def delete_session(self, session_id: str) -> bool:
        """Delete an Agent SDK session."""
        await sdk_delete_session(session_id)
        return True


# Singleton
agent_client = AgentClient()
