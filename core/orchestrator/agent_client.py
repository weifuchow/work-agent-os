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
                (input["event_type"], input.get("target_type", ""), input.get("target_id", ""), input["detail"], "agent", datetime.now(UTC).isoformat()),
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
    updates["updated_at"] = datetime.now(UTC).isoformat()
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
    cutoff = (datetime.now(UTC) - timedelta(hours=2)).isoformat()

    try:
        async with aiosqlite.connect(str(db_path)) as db:
            db.row_factory = aiosqlite.Row

            # Strategy 1: same chat_id + project + active within 2h
            if project:
                cursor = await db.execute(
                    "SELECT id, title, topic, project FROM sessions "
                    "WHERE source_chat_id = ? AND project = ? AND status IN ('open','waiting') "
                    "AND last_active_at >= ? ORDER BY last_active_at DESC LIMIT 1",
                    (chat_id, project, cutoff),
                )
                row = await cursor.fetchone()
                if row:
                    await _attach_msg_to_session(db, row["id"], input["message_id"])
                    return {"session_id": row["id"], "title": row["title"], "action": "matched_by_project"}

            # Strategy 2: same chat_id + active window
            cursor = await db.execute(
                "SELECT id, title, topic, project FROM sessions "
                "WHERE source_chat_id = ? AND status IN ('open','waiting') "
                "AND last_active_at >= ? ORDER BY last_active_at DESC LIMIT 1",
                (chat_id, cutoff),
            )
            row = await cursor.fetchone()
            if row:
                await _attach_msg_to_session(db, row["id"], input["message_id"])
                return {"session_id": row["id"], "title": row["title"], "action": "matched_by_chat"}

            # Strategy 3: create new session
            now = datetime.now(UTC)
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
                        (match_id, datetime.now(UTC).isoformat(), session_id),
                    )
                    await db.execute(
                        "UPDATE task_contexts SET updated_at = ? WHERE id = ?",
                        (datetime.now(UTC).isoformat(), match_id),
                    )
                    await db.commit()
                    return {"task_context_id": match_id, "title": row["title"], "action": "linked_existing"}

            # Create new task context
            now = datetime.now(UTC)
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
    now = datetime.now(UTC).isoformat()

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


# ==================== MCP Server ====================

CUSTOM_TOOLS = [query_db, write_audit_log, send_feishu_message, read_memory, write_memory,
                update_session, route_to_session, link_task_context]

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
            now = datetime.now(UTC).isoformat()
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
    ) -> ClaudeAgentOptions:
        from skills import SKILL_REGISTRY

        builtin_tools = ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]

        opts = ClaudeAgentOptions(
            system_prompt=system_prompt or None,
            mcp_servers={"work-agent-tools": CUSTOM_MCP_SERVER},
            allowed_tools=builtin_tools + CUSTOM_TOOL_NAMES,
            permission_mode="bypassPermissions",
            max_turns=max_turns,
            cwd=str(PROJECT_ROOT),
            env=self._get_env(),
            # Register all skills as sub-agents
            agents=SKILL_REGISTRY,
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
