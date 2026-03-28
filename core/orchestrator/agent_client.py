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


# ==================== MCP Server ====================

CUSTOM_TOOLS = [query_db, write_audit_log, send_feishu_message, read_memory, write_memory, update_session]

CUSTOM_MCP_SERVER = create_sdk_mcp_server(
    name="work-agent-tools",
    version="1.0.0",
    tools=CUSTOM_TOOLS,
)

CUSTOM_TOOL_NAMES = [f"mcp__work-agent-tools__{t.name}" for t in CUSTOM_TOOLS]


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
