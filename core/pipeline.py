"""Message processing pipeline.

Two-level agent architecture:
- Main Agent: classifies messages, dispatches to project agents
- Project Agent: executes in project directory with full context

Pipeline handles ALL IO (feishu reply, DB writes). Agent only outputs JSON.
All DB access uses raw SQL (aiosqlite) to avoid ORM cache issues.
"""

import asyncio
import json
import uuid
from datetime import datetime
from pathlib import Path

import aiosqlite
from loguru import logger

from core.config import settings, get_agent_runtime_override
from core.orchestrator.agent_runtime import (
    DEFAULT_AGENT_RUNTIME,
    get_agent_run_runtime_type,
    normalize_agent_runtime,
)

DB_PATH = str(Path(settings.db_dir) / "app.sqlite")


# ---------------------------------------------------------------------------
# Per-session lock registry — serialize messages within the same session
# ---------------------------------------------------------------------------

_session_locks: dict[int, asyncio.Lock] = {}
_session_locks_guard = asyncio.Lock()


async def _get_session_lock(session_id: int) -> asyncio.Lock:
    async with _session_locks_guard:
        if session_id not in _session_locks:
            _session_locks[session_id] = asyncio.Lock()
        return _session_locks[session_id]


# ---------------------------------------------------------------------------
# System Prompt (template — projects injected at runtime)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT_TEMPLATE = """你是用户的私人工作助理，通过飞书接收消息。

## 已注册项目
{projects_section}

## 核心规则

1. **项目匹配优先**：如果消息涉及上方任何项目（按名称/别名/关键词匹配），你**必须调用 `dispatch_to_project` tool**，将 dispatch 返回的结果作为 reply_content。**绝对不能跳过 dispatch 用项目描述自行回答。**
2. **所有消息必须回复**：无论是闲聊、问候、表情、系统通知，还是工作问题，都必须生成 reply_content 进行回复。
3. 高风险操作（承诺排期/确认上线/技术拍板）使用 action=drafted。

## 输出格式

最终输出必须是合法 JSON：
```json
{{
  "action": "replied | drafted",
  "classified_type": "chat | work_question | urgent_issue | task_request | noise",
  "topic": "主题",
  "project_name": "项目名或null",
  "reply_content": "回复内容（必填）",
  "reason": "处理理由"
}}
```
- reply_content 必填，系统用它发送飞书消息
"""


def _build_system_prompt() -> str:
    from core.projects import get_projects
    projects = get_projects()
    lines = []
    for p in projects:
        desc = p.description.replace("\n", " ").strip()
        lines.append(f"- **{p.name}**: {desc}")
    section = "\n".join(lines) if lines else "（暂无注册项目）"
    return SYSTEM_PROMPT_TEMPLATE.format(projects_section=section)


def _get_default_agent_runtime() -> str:
    return normalize_agent_runtime(
        get_agent_runtime_override() or settings.default_agent_runtime or DEFAULT_AGENT_RUNTIME
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def process_message(message_id: int) -> None:
    """Process a single message end-to-end.

    Messages for the same session are serialized via per-session locks to
    prevent concurrent agent resume calls on the same SDK session.
    """
    try:
        # 1. Read message
        msg = await _read_message(message_id)
        if not msg or msg["pipeline_status"] == "completed":
            return

        await _update_message(message_id, pipeline_status="classifying")

        # 2. Session routing (thread_id match or create new) — outside lock
        session_id = msg["session_id"] or await _route_session(msg)
        if session_id:
            await _update_message(message_id, session_id=session_id)
            await _attach_message_to_session(message_id, session_id)

        # 3. Acquire per-session lock, then run agent + deliver + persist
        if session_id:
            lock = await _get_session_lock(session_id)
            logger.debug("Pipeline: message {} waiting for session {} lock", message_id, session_id)
            async with lock:
                logger.debug("Pipeline: message {} acquired session {} lock", message_id, session_id)
                await _process_message_locked(message_id, msg, session_id)
        else:
            await _process_message_locked(message_id, msg, session_id)

    except Exception as e:
        logger.exception("Pipeline failed for message {}: {}", message_id, e)
        await _update_message(message_id,
                              pipeline_status="failed",
                              pipeline_error=str(e)[:500],
                              processed_at=datetime.now().isoformat())
        await _audit("pipeline_failed", "message", str(message_id),
                      {"error": str(e)[:1000]})


async def _process_message_locked(message_id: int, msg: dict,
                                   session_id: int | None) -> None:
    """Core processing under per-session lock (steps 3-11)."""
    # 3. Re-read fresh session state (may have been updated by prior message)
    session = await _read_session(session_id) if session_id else None

    # 4. Audit: log before agent call
    prompt = _build_prompt(msg, session)
    await _audit("pipeline_agent_call", "message", str(message_id), {
        "message_id": message_id,
        "session": session,
        "prompt": prompt[:2000],
    })

    # 5. Run agent (project resume or orchestrator)
    agent_sid = session["agent_session_id"] if session else None
    project = session["project"] if session else ""
    agent_runtime = normalize_agent_runtime(
        (session or {}).get("agent_runtime") or _get_default_agent_runtime()
    )

    if agent_sid and project:
        result = await _run_project_agent(project, prompt, agent_sid, runtime=agent_runtime)
        # Fallback: context overflow → compact session then retry
        if (
            agent_runtime == "claude"
            and result.get("is_error")
            and result.get("text") == "Prompt is too long"
        ):
            from core.projects import get_project as _get_proj
            proj = _get_proj(project)
            cwd = str(proj.path) if proj else None
            if cwd:
                logger.warning("Pipeline: session {} context overflow, compacting...", agent_sid)
                compacted = await _compact_agent_session(agent_sid, cwd)
                if compacted:
                    await _audit("context_compacted", "session", str(session_id),
                                  {"agent_session_id": agent_sid, "trigger": "overflow"})
                    result = await _run_project_agent(project, prompt, agent_sid)
                else:
                    logger.error("Pipeline: compact failed, cannot recover session {}", agent_sid)
    else:
        result = await _run_orchestrator(prompt, session_id=agent_sid, runtime=agent_runtime)

    result_text = result.get("text", "")
    new_agent_sid = result.get("session_id")
    parsed = _parse_result(result_text) if not (agent_sid and project) else {
        "action": "replied",
        "classified_type": "work_question",
        "topic": session["title"] if session else "",
        "project_name": project,
        "reply_content": result_text,
        "reason": "resumed project session",
    }

    # 6. Audit: log agent result
    # Read effective agent_session_id from DB (dispatch_to_project may have
    # written the project agent's session_id directly, which differs from
    # the orchestrator's own session_id returned in new_agent_sid).
    effective_agent_sid = new_agent_sid
    if session_id:
        fresh = await _read_session(session_id)
        if fresh and fresh.get("agent_session_id"):
            effective_agent_sid = fresh["agent_session_id"]

    await _audit("pipeline_agent_result", "message", str(message_id), {
        "agent_session_id": effective_agent_sid,
        "action": parsed.get("action"),
        "classified_type": parsed.get("classified_type"),
        "result_text": result_text[:1500],
    })

    # 7. Deliver reply via feishu (pipeline handles this, not agent)
    reply_content = parsed.get("reply_content", "")
    action = parsed.get("action", "replied")
    thread_id = None
    delivered = True

    if action in ("replied", "drafted"):
        if not reply_content:
            # Agent said replied but gave no content — treat as failure
            await _update_message(message_id,
                                  pipeline_status="failed",
                                  pipeline_error="action=replied but reply_content is empty",
                                  processed_at=datetime.now().isoformat())
            await _audit("pipeline_feishu_reply", "message", str(message_id), {
                "delivered": False,
                "action": action,
                "error": "empty reply_content",
            })
            logger.warning("Pipeline: message {} marked failed — empty reply_content", message_id)
            return
        if not msg["platform_message_id"]:
            await _update_message(message_id,
                                  pipeline_status="failed",
                                  pipeline_error="missing platform_message_id, cannot deliver",
                                  processed_at=datetime.now().isoformat())
            logger.warning("Pipeline: message {} marked failed — no platform_message_id", message_id)
            return
        thread_id, delivered = await _deliver_reply(msg, reply_content, session_id)
        await _audit("pipeline_feishu_reply", "message", str(message_id), {
            "delivered": delivered,
            "action": action,
            "thread_id": thread_id,
            "content_length": len(reply_content),
        })
        if not delivered:
            await _update_message(message_id,
                                  pipeline_status="failed",
                                  pipeline_error="feishu reply delivery failed",
                                  processed_at=datetime.now().isoformat())
            logger.warning("Pipeline: message {} marked failed — feishu delivery failed", message_id)
            return

    # 8. Persist session state (agent_session_id, project, thread_id)
    if session_id:
        await _update_session_state(
            session_id,
            agent_session_id=new_agent_sid,
            agent_runtime=agent_runtime,
            project=parsed.get("project_name") or "",
            thread_id=thread_id,
        )

    # 9. Mark completed
    await _update_message(message_id,
                          pipeline_status="completed",
                          classified_type=parsed.get("classified_type", "chat"),
                          processed_at=datetime.now().isoformat(),
                          pipeline_error="")

    # 10. Record agent run
    await _record_agent_run(message_id, session_id, new_agent_sid, agent_runtime, result)

    await _audit("pipeline_completed", "message", str(message_id), {
        "action": action,
        "classified_type": parsed.get("classified_type"),
        "session_id": session_id,
    })
    logger.info("Pipeline: message {} → {} ({})",
                 message_id, action, parsed.get("classified_type"))

    # 11. Proactive compaction — also under session lock (runs in background
    #     but acquires the same lock so it won't overlap with the next message)
    effective_sid = effective_agent_sid or new_agent_sid
    if effective_sid and project and agent_runtime == "claude":
        input_tokens = _get_input_tokens(result)
        ctx_window = _get_context_window(result.get("model"))
        if input_tokens > ctx_window * _COMPACT_THRESHOLD:
            from core.projects import get_project as _get_proj
            proj = _get_proj(project)
            if proj:
                logger.info("Pipeline: proactive compact for session {} "
                            "(tokens={}, threshold={})",
                            effective_sid, input_tokens, int(ctx_window * _COMPACT_THRESHOLD))
                asyncio.create_task(
                    _proactive_compact_locked(session_id, effective_sid, str(proj.path))
                )


async def reprocess_message(message_id: int) -> None:
    await _update_message(message_id,
                          pipeline_status="pending",
                          pipeline_error="",
                          classified_type=None,
                          session_id=None,
                          processed_at=None)
    await process_message(message_id)


# ---------------------------------------------------------------------------
# Context compaction
# ---------------------------------------------------------------------------

# Model context window sizes (tokens)
_CONTEXT_WINDOWS = {
    "haiku": 200_000,
    "sonnet": 200_000,
    "opus": 1_000_000,
}
_COMPACT_THRESHOLD = 0.6  # Compact when input exceeds 60% of context window
_COMPACT_MODEL = "claude-opus-4-6"  # Large model used for compaction


def _get_context_window(model_id: str | None) -> int:
    """Estimate context window from model id string."""
    if not model_id:
        return 200_000
    model_lower = model_id.lower()
    for key, size in _CONTEXT_WINDOWS.items():
        if key in model_lower:
            return size
    return 200_000  # Conservative default


async def _compact_agent_session(session_id: str, cwd: str,
                                  model: str | None = None) -> bool:
    """Send /compact to an agent session via SDK. Returns True on success."""
    from claude_agent_sdk import query as sdk_query, ClaudeAgentOptions, ResultMessage

    opts = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        max_turns=10,
        cwd=cwd,
        model=model or _COMPACT_MODEL,
    )
    opts.resume = session_id

    try:
        async for msg in sdk_query(prompt="/compact", options=opts):
            if isinstance(msg, ResultMessage):
                if msg.is_error:
                    logger.error("Compact failed for session {}: is_error=True", session_id)
                    return False
        logger.info("Compact completed for session {}", session_id)
        return True
    except Exception as e:
        logger.error("Compact exception for session {}: {}", session_id, e)
        return False


async def _proactive_compact_locked(db_session_id: int,
                                     agent_session_id: str, cwd: str) -> None:
    """Background task: acquire session lock, then compact."""
    lock = await _get_session_lock(db_session_id)
    async with lock:
        try:
            ok = await _compact_agent_session(agent_session_id, cwd)
            if ok:
                await _audit("context_compacted", "session", str(db_session_id),
                              {"agent_session_id": agent_session_id, "trigger": "proactive"})
        except Exception as e:
            logger.error("Proactive compact failed for {}: {}", agent_session_id, e)


def _get_input_tokens(result: dict) -> int:
    """Extract total input tokens from agent result usage."""
    usage = result.get("usage", {})
    return (usage.get("input_tokens", 0)
            + usage.get("cache_read_input_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0))


# ---------------------------------------------------------------------------
# Agent execution
# ---------------------------------------------------------------------------

async def _run_orchestrator(
    prompt: str,
    session_id: str | None = None,
    runtime: str | None = None,
) -> dict:
    from core.orchestrator.agent_client import agent_client
    return await agent_client.run(
        prompt=prompt,
        system_prompt=_build_system_prompt(),
        max_turns=30,
        session_id=session_id,
        runtime=runtime,
    )


async def _run_project_agent(
    project_name: str,
    prompt: str,
    session_id: str,
    runtime: str | None = None,
) -> dict:
    from core.orchestrator.agent_client import agent_client
    from core.projects import get_project, merge_skills
    from skills import SKILL_REGISTRY

    project = get_project(project_name)
    if not project:
        logger.warning("Project {} not found, falling back to orchestrator", project_name)
        return await _run_orchestrator(prompt, runtime=runtime)

    merged = merge_skills(SKILL_REGISTRY, project.path)
    system = f"你运行在项目 {project.name} 的工作目录中（{project.path}）。处理用户的请求。"

    logger.info("_run_project_agent: project={}, session_id={}, cwd={}, prompt={}",
                project_name, session_id, str(project.path), prompt[:100])

    return await agent_client.run_for_project(
        prompt=prompt,
        system_prompt=system,
        project_cwd=str(project.path),
        project_agents=merged,
        max_turns=20,
        session_id=session_id,
        runtime=runtime,
    )


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------

def _build_prompt(msg: dict, session: dict | None) -> str:
    """Build prompt from message and session context."""
    content = msg["content"] or ""
    media = _extract_media(msg)

    # Project resume: just the user message
    if session and session.get("agent_session_id") and session.get("project"):
        prompt = content
        if media:
            prompt += f"\n\n[多模态内容] {media}"
        return prompt

    # New or non-project: include IDs for orchestrator
    parts = [f"飞书消息ID: {msg['platform_message_id']} | db_session_id: {session['id'] if session else ''}"]
    if media:
        parts.append(f"[多模态内容] {media}")
    parts.append("")
    parts.append(content)
    return "\n".join(parts)


def _extract_media(msg: dict) -> str:
    if msg.get("message_type") in ("text", None, ""):
        return ""
    content = msg.get("content", "")
    label = content[1:-1] if content.startswith("[") else (msg.get("message_type", "") + "消息")
    return f"用户发送了{label}"


# ---------------------------------------------------------------------------
# Session routing
# ---------------------------------------------------------------------------

async def _route_session(msg: dict) -> int | None:
    """Match message to session by thread_id or create new."""
    if not msg.get("chat_id"):
        return None

    async with aiosqlite.connect(DB_PATH) as db:
        # Match by thread_id
        if msg.get("thread_id"):
            cursor = await db.execute(
                "SELECT id FROM sessions WHERE thread_id = ? AND status IN ('open','waiting') "
                "ORDER BY last_active_at DESC LIMIT 1",
                (msg["thread_id"],),
            )
            row = await cursor.fetchone()
            if row:
                return row[0]

        # Create new session
        now = datetime.now().isoformat()
        agent_runtime = _get_default_agent_runtime()
        key = f"feishu_{msg['chat_id'][:16]}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        title = (msg.get("content") or "新会话")[:64]
        cursor = await db.execute(
            "INSERT INTO sessions (session_key, source_platform, source_chat_id, owner_user_id, "
            "title, topic, project, priority, status, thread_id, agent_runtime, summary_path, "
            "last_active_at, message_count, risk_level, needs_manual_review, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (key, "feishu", msg["chat_id"], msg.get("sender_id", ""),
             title, "", "", "normal", "open", msg.get("thread_id", ""), agent_runtime, "",
             now, 0, "low", False, now, now),
        )
        await db.commit()
        return cursor.lastrowid


async def _attach_message_to_session(message_id: int, session_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        # Update session counters
        now = datetime.now().isoformat()
        await db.execute(
            "UPDATE sessions SET message_count = message_count + 1, "
            "last_active_at = ?, updated_at = ? WHERE id = ?",
            (now, now, session_id),
        )
        # Add session_message link
        cursor = await db.execute(
            "SELECT COALESCE(MAX(sequence_no), 0) FROM session_messages WHERE session_id = ?",
            (session_id,),
        )
        max_seq = (await cursor.fetchone())[0]
        await db.execute(
            "INSERT INTO session_messages (session_id, message_id, role, sequence_no, created_at) "
            "VALUES (?,?,?,?,?)",
            (session_id, message_id, "user", max_seq + 1, now),
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Reply delivery (feishu + DB)
# ---------------------------------------------------------------------------

async def _deliver_reply(msg: dict, content: str, session_id: int | None) -> tuple[str | None, bool]:
    """Send reply to feishu and save to DB. Returns (thread_id, delivered)."""
    delivered = False
    thread_id = ""
    try:
        from core.connectors.feishu import FeishuClient
        client = FeishuClient()
        result = client.reply_message(
            message_id=msg["platform_message_id"],
            content=content[:4000],
            reply_in_thread=True,
        )
        thread_id = result.get("thread_id", "") if result else ""
        delivered = True
        logger.info("Pipeline: delivered reply for message {}", msg["id"])
    except Exception as e:
        logger.warning("Pipeline: feishu reply failed for message {}: {}", msg["id"], e)

    # Save bot reply to DB
    try:
        now = datetime.now().isoformat()
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT OR IGNORE INTO messages (platform, platform_message_id, chat_id, "
                "sender_id, sender_name, message_type, content, received_at, raw_payload, "
                "thread_id, root_id, parent_id, classified_type, "
                "session_id, pipeline_status, pipeline_error, processed_at, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (msg.get("platform", "feishu"), f"reply_{msg['platform_message_id']}",
                 msg["chat_id"], "bot", "WorkAgent", "text", content[:4000],
                 now, "",
                 thread_id or "", "", "", "bot_reply",
                 session_id, "completed", "", now, now),
            )
            await db.commit()
    except Exception as e:
        logger.warning("Pipeline: save bot reply failed: {}", e)

    return thread_id or None, delivered


# ---------------------------------------------------------------------------
# Session state persistence
# ---------------------------------------------------------------------------

async def _update_session_state(session_id: int, *,
                                 agent_session_id: str | None = None,
                                 agent_runtime: str | None = None,
                                 project: str = "",
                                 thread_id: str | None = None) -> None:
    """Write agent session metadata to session (only if not already set)."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT agent_session_id, agent_runtime, project, thread_id FROM sessions WHERE id = ?",
            (session_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return

        current_sid, current_runtime, current_project, current_thread = row
        updates, params = [], []

        if not current_sid and agent_session_id:
            updates.append("agent_session_id = ?")
            params.append(agent_session_id)

        if (not current_runtime or current_runtime == DEFAULT_AGENT_RUNTIME) and agent_runtime:
            updates.append("agent_runtime = ?")
            params.append(normalize_agent_runtime(agent_runtime))

        if not current_project and project:
            updates.append("project = ?")
            params.append(project)

        if not current_thread and thread_id:
            updates.append("thread_id = ?")
            params.append(thread_id)
            logger.info("Session {} bound to thread_id {}", session_id, thread_id)

        if updates:
            updates.append("updated_at = ?")
            params.append(datetime.now().isoformat())
            params.append(session_id)
            await db.execute(
                f"UPDATE sessions SET {', '.join(updates)} WHERE id = ?", params,
            )
            await db.commit()


# ---------------------------------------------------------------------------
# DB helpers (all raw SQL)
# ---------------------------------------------------------------------------

async def _read_message(message_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM messages WHERE id = ?", (message_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def _update_message(message_id: int, **fields) -> None:
    if not fields:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        await db.execute(
            f"UPDATE messages SET {set_clause} WHERE id = ?",
            [*fields.values(), message_id],
        )
        await db.commit()


async def _read_session(session_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def _audit(event_type: str, target_type: str, target_id: str,
                 detail: dict | str) -> None:
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO audit_logs (event_type, target_type, target_id, detail, "
                "operator, created_at) VALUES (?,?,?,?,?,?)",
                (event_type, target_type, target_id,
                 json.dumps(detail, ensure_ascii=False) if isinstance(detail, dict) else detail,
                 "orchestrator", datetime.now().isoformat()),
            )
            await db.commit()
    except Exception as e:
        logger.warning("Audit log failed: {}", e)


async def _record_agent_run(message_id: int, session_id: int | None,
                             agent_session_id: str | None,
                             agent_runtime: str,
                             result: dict) -> None:
    try:
        now = datetime.now().isoformat()
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO agent_runs (message_id, agent_name, runtime_type, "
                "session_id, status, started_at, ended_at, cost_usd, "
                "input_path, output_path, input_tokens, output_tokens, error_message) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (message_id, "orchestrator", get_agent_run_runtime_type(agent_runtime), session_id, "success",
                 now, now, result.get("cost_usd", 0),
                 f"agent_session:{agent_session_id}" if agent_session_id else "",
                 result.get("text", "")[:2000],
                 result.get("num_turns", 0), 0, ""),
            )
            await db.commit()
    except Exception as e:
        logger.warning("Record agent run failed: {}", e)


# ---------------------------------------------------------------------------
# JSON parsing
# ---------------------------------------------------------------------------

def _parse_result(text: str) -> dict:
    """Extract JSON with 'action' key from agent output."""
    text = text.strip()
    if not text:
        return _fallback("empty output")

    def _valid(d):
        return isinstance(d, dict) and "action" in d

    # Try: whole text
    try:
        d = json.loads(text)
        if _valid(d):
            return d
    except (json.JSONDecodeError, ValueError):
        pass

    # Try: fenced ```json blocks
    if "```" in text:
        for block in text.split("```"):
            block = block.strip()
            if block.startswith("json"):
                block = block[4:].strip()
            try:
                d = json.loads(block)
                if _valid(d):
                    return d
            except (json.JSONDecodeError, ValueError):
                continue

    # Try: last balanced { } containing "action"
    pos = len(text)
    while pos > 0:
        end = text.rfind("}", 0, pos)
        if end == -1:
            break
        depth, start = 0, end
        while start >= 0:
            if text[start] == "}":
                depth += 1
            elif text[start] == "{":
                depth -= 1
                if depth == 0:
                    break
            start -= 1
        if start >= 0 and depth == 0:
            try:
                d = json.loads(text[start:end + 1])
                if _valid(d):
                    return d
            except (json.JSONDecodeError, ValueError):
                pass
        pos = start

    # Parse failed but agent produced text — use it directly as reply
    if text:
        logger.warning("Pipeline: JSON parse failed, using raw text as reply (len={})", len(text))
        return {
            "action": "replied",
            "classified_type": "chat",
            "topic": "",
            "project_name": None,
            "reply_content": text,
            "reason": "parse_failed_fallback",
        }

    return _fallback("failed to parse")


def _fallback(reason: str) -> dict:
    return {"action": "replied", "classified_type": "chat", "topic": "",
            "reply_content": "", "reason": reason}
