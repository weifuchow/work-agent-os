"""MCP tool definitions and tool-scope registries for agent runtimes."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool
from loguru import logger

from core.config import settings
from core.orchestrator.agent_runtime import get_agent_run_runtime_type, normalize_agent_runtime

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_SESSION_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
_DEFAULT_PROJECT_DISPATCH_TIMEOUT_SECONDS = 1800
_RIOT_LOG_TRIAGE_SKILL = "riot-log-triage"
_RIOT_LOG_TRIAGE_PROJECTS = {"allspark", "riot-standalone", "fms-java"}
_RIOT_DIRECT_TRIAGE_MARKERS = (
    "日志",
    "log",
    "ones",
    "工单",
    "现场",
    "附件",
    "上传",
    "图片",
    "截图",
    "报错",
    "异常",
    "堆栈",
    "trace",
    "error",
    "exception",
)
_RIOT_EXECUTION_TRIAGE_MARKERS = (
    "订单",
    "任务",
    "子任务",
    "车辆",
    "agv",
    "seeragv",
    "seer",
    "自由导航",
    "导航",
    "路径",
    "点位",
    "站点",
    "工位",
    "库位",
    "下发",
    "经过",
    "未经过",
    "没经过",
    "跳过",
    "绕行",
    "死锁",
    "避让",
    "交通",
    "门禁",
    "电梯",
    "充电",
    "卡住",
    "超时",
    "挂起",
    "恢复",
    "状态流转",
    "执行链路",
)
_INVESTIGATION_INTENT_MARKERS = ("检查", "排查", "分析", "定位", "为什么", "为何", "原因", "看一下", "看看", "查一下")
_NEGATED_RIOT_TRIAGE_MARKERS = (
    "不涉及日志",
    "不涉及现场日志",
    "不用日志",
    "不看日志",
    "不要看日志",
    "不分析日志",
    "不是日志",
    "无日志",
    "没有日志",
    "不涉及订单",
    "不涉及车辆",
    "不需要排障",
)
_LOG_LIKE_UPLOAD_SUFFIXES = {".log", ".gz", ".zip", ".tar", ".tgz", ".txt", ".json"}


def _project_root() -> Path:
    """Return the active project root, honoring legacy monkeypatches on agent_client."""

    try:
        from core.orchestrator import agent_client as agent_client_mod

        return Path(getattr(agent_client_mod, "PROJECT_ROOT", PROJECT_ROOT))
    except Exception:
        return PROJECT_ROOT


def _app_db_path() -> Path:
    replay_db = str(os.environ.get("WORK_AGENT_REPLAY_DB_PATH") or "").strip()
    if replay_db:
        return Path(replay_db)
    return _project_root() / "data" / "db" / "app.sqlite"


def _sessions_root() -> Path:
    replay_sessions = str(os.environ.get("WORK_AGENT_REPLAY_SESSIONS_DIR") or "").strip()
    if replay_sessions:
        return Path(replay_sessions)
    return _project_root() / "data" / "sessions"


def _session_workspace_path(db_session_id: int) -> Path:
    return _sessions_root() / f"session-{int(db_session_id)}" / "session_workspace.json"


def _message_artifact_segment(message_id: Any) -> str:
    try:
        value = int(message_id)
    except (TypeError, ValueError):
        value = 0
    return f"message-{value}" if value > 0 else "message-unknown"


def _safe_artifact_segment(value: Any, *, fallback: str = "item") -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9._-]+", "-", text)
    text = text.strip(".-")
    return text or fallback


def _write_json_artifact(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _read_json_artifact(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _session_upload_image_paths(artifact_roots: dict[str, str], *, max_images: int = 6) -> list[str]:
    uploads_text = str(artifact_roots.get("uploads_dir") or "").strip()
    if not uploads_text:
        return []
    uploads_dir = Path(uploads_text)
    try:
        candidates = [
            path
            for path in uploads_dir.iterdir()
            if path.is_file() and path.suffix.lower() in _SESSION_IMAGE_EXTENSIONS
        ]
    except OSError:
        return []

    def _mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    image_paths: list[str] = []
    for path in sorted(candidates, key=_mtime, reverse=True)[:max_images]:
        try:
            image_paths.append(str(path.resolve()))
        except OSError:
            image_paths.append(str(path))
    return image_paths


def _session_upload_file_paths(artifact_roots: dict[str, str], *, max_files: int = 20) -> list[Path]:
    uploads_text = str(artifact_roots.get("uploads_dir") or "").strip()
    if not uploads_text:
        return []
    uploads_dir = Path(uploads_text)
    try:
        candidates = [path for path in uploads_dir.iterdir() if path.is_file()]
    except OSError:
        return []

    def _mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    return sorted(candidates, key=_mtime, reverse=True)[:max_files]


def _has_session_uploads(artifact_roots: dict[str, str]) -> bool:
    return bool(_session_upload_file_paths(artifact_roots, max_files=1))


def _has_log_like_session_uploads(artifact_roots: dict[str, str]) -> bool:
    for path in _session_upload_file_paths(artifact_roots):
        name = path.name.lower()
        if any(name.endswith(suffix) for suffix in _LOG_LIKE_UPLOAD_SUFFIXES):
            return True
        if ".log." in name or name.endswith(".log"):
            return True
    return False


def _text_contains_any(text: str, markers: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(marker.lower() in lowered for marker in markers)


def _looks_like_riot_log_triage_task(
    *,
    project_name: str,
    task: str,
    context: str,
    artifact_roots: dict[str, str],
) -> bool:
    """Detect RIOT troubleshooting turns where the log triage workflow is required.

    This intentionally uses only the current dispatch payload and session uploads.
    Existing .triage state is ignored so stale analysis workspaces cannot force a
    skill selection for unrelated project questions.
    """

    if project_name.strip().lower() not in _RIOT_LOG_TRIAGE_PROJECTS:
        return False

    text = f"{task}\n{context}".strip()
    if not text:
        return False
    if _text_contains_any(text, _NEGATED_RIOT_TRIAGE_MARKERS):
        return False

    has_uploads = _has_session_uploads(artifact_roots)
    has_log_uploads = _has_log_like_session_uploads(artifact_roots)
    has_investigation_intent = _text_contains_any(text, _INVESTIGATION_INTENT_MARKERS)

    if _text_contains_any(text, _RIOT_DIRECT_TRIAGE_MARKERS) and (
        has_investigation_intent or has_uploads or has_log_uploads
    ):
        return True

    execution_related = _text_contains_any(text, _RIOT_EXECUTION_TRIAGE_MARKERS)
    if not execution_related:
        return False

    if has_investigation_intent:
        return True

    return has_uploads or has_log_uploads


def _skill_registered(skill_name: str) -> bool:
    try:
        from core.skill_registry import SKILL_REGISTRY as registry

        if skill_name in registry:
            return True
    except Exception:
        pass
    return (_project_root() / ".claude" / "skills" / skill_name / "SKILL.md").is_file()


def _project_dispatch_timeout_seconds() -> int:
    raw = str(os.environ.get("PROJECT_DISPATCH_TIMEOUT_SECONDS") or "").strip()
    if raw:
        try:
            configured = int(raw)
        except ValueError:
            configured = 0
        if configured > 0:
            return configured
    return _DEFAULT_PROJECT_DISPATCH_TIMEOUT_SECONDS


def _extract_message_id(input_payload: dict[str, Any]) -> int | None:
    for key in ("message_id", "db_message_id"):
        try:
            value = int(input_payload.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    return None


def _dispatch_id(dispatch_run_id: int | None) -> str:
    if dispatch_run_id:
        return f"dispatch-{int(dispatch_run_id):03d}"
    return "dispatch-untracked"


def _project_entry_from_workspace(
    project_workspace: dict[str, Any] | None,
    project_name: str,
) -> dict[str, Any]:
    projects = project_workspace.get("projects") if isinstance(project_workspace, dict) else None
    entry = projects.get(project_name) if isinstance(projects, dict) else None
    return entry if isinstance(entry, dict) else {}


def _triage_dir_for_analysis_workspace(value: str) -> str:
    if not value:
        return ""
    path = Path(value)
    try:
        if (path / "00-state.json").exists():
            return str(path)
    except OSError:
        return ""
    return ""


def _project_entry_is_ready(entry: dict[str, Any]) -> bool:
    path_text = str(entry.get("worktree_path") or entry.get("execution_path") or "").strip()
    if not path_text:
        return False
    status = str(entry.get("status") or "").strip().lower()
    if status and status not in {"ready", "loaded"}:
        return False
    try:
        return Path(path_text).exists()
    except OSError:
        return False


def _dispatch_triage_dir(
    *,
    analysis_workspace: str,
    artifact_roots: dict[str, str],
    requested_skill: str,
    selected_skill: str,
    message_segment: str,
    project_segment: str,
    dispatch_id: str,
) -> str:
    existing = _triage_dir_for_analysis_workspace(analysis_workspace)
    if existing:
        return existing
    if (selected_skill or requested_skill) != "riot-log-triage":
        return ""
    triage_root = str(artifact_roots.get("triage_dir") or "").strip()
    if not triage_root:
        return ""
    return str(Path(triage_root) / f"{message_segment}-{project_segment}-{dispatch_id}")


def _ensure_triage_preflight(
    *,
    triage_dir: str,
    project_name: str,
    message_id: int | None,
    dispatch_id: str,
    task: str,
    context: str,
    analysis_dir: Path | None,
    artifact_roots: dict[str, str],
) -> None:
    if not triage_dir:
        return
    base = Path(triage_dir)
    try:
        (base / "01-intake" / "messages").mkdir(parents=True, exist_ok=True)
        (base / "01-intake" / "attachments").mkdir(parents=True, exist_ok=True)
        (base / "02-process").mkdir(parents=True, exist_ok=True)
        (base / "search-runs").mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("Failed to create triage preflight directories: {}", exc)
        return

    state_path = base / "00-state.json"
    if not state_path.exists():
        _write_json_artifact(
            state_path,
            {
                "project": project_name,
                "mode": "structured",
                "phase": "preflight",
                "message_id": message_id,
                "dispatch_id": dispatch_id,
                "primary_question": task,
                "current_question": task,
                "artifact_status": "preflight",
                "analysis_dir": str(analysis_dir or ""),
                "artifact_roots": artifact_roots,
                "agent_context": {
                    "runtime": "project-dispatch",
                    "skill": "riot-log-triage",
                },
                "missing_items": [],
            },
        )

    _write_json_artifact(
        base / "01-intake" / "messages" / "dispatch_input.json",
        {
            "message_id": message_id,
            "dispatch_id": dispatch_id,
            "project": project_name,
            "task": task,
            "context": context,
            "analysis_dir": str(analysis_dir or ""),
        },
    )


def _persist_project_run_record(
    *,
    session_workspace_path: Path | None,
    project_name: str,
    message_id: int | None,
    dispatch_id: str,
    skill: str,
    analysis_dir: Path | None,
    agent_session_id: str,
    status: str,
) -> dict[str, Any]:
    if not session_workspace_path:
        return {}
    try:
        from core.app.project_workspace import append_project_agent_run

        return append_project_agent_run(
            session_workspace_path=session_workspace_path,
            project_name=project_name,
            run={
                "message_id": message_id,
                "dispatch_id": dispatch_id,
                "skill": skill,
                "analysis_dir": str(analysis_dir) if analysis_dir else "",
                "agent_session_id": agent_session_id,
                "status": status,
            },
        )
    except Exception as exc:
        logger.warning("Failed to append project agent run record: {}", exc)
        return {}


def _write_project_dispatch_failure(
    *,
    project_name: str,
    dispatch_id: str,
    error_text: str,
    result_artifact: Path | None,
    trace_artifact: Path | None,
    dispatch_artifact: Path | None,
    triage_dir: str,
    result_status: str = "failed",
) -> None:
    if result_artifact:
        _write_json_artifact(
            result_artifact,
            {
                "project": project_name,
                "dispatch_id": dispatch_id,
                "failed": True,
                "status": result_status,
                "error": error_text,
                "project_agent_session_id": "",
                "project_agent_session_record_only": True,
                "triage_dir": triage_dir,
                "structured_evidence_summary": [],
            },
        )
    if trace_artifact:
        trace_artifact.write_text(
            f"# Project Analysis Trace\n\n- status: {result_status}\n- project: {project_name}\n- dispatch_id: {dispatch_id}\n- error: {error_text}\n",
            encoding="utf-8",
        )
    if dispatch_artifact:
        payload = _read_json_artifact(dispatch_artifact)
        payload.update({
            "status": result_status,
            "error": error_text,
            "result_path": str(result_artifact or ""),
        })
        _write_json_artifact(dispatch_artifact, payload)


def _is_correction_turn(*, task: str, context: str) -> bool:
    text = f"{task}\n{context}"
    correction_markers = ["干扰项", "不是", "纠正", "更正", "修正", "忽略上次", "重新看"]
    return any(marker in text for marker in correction_markers)

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
    "write_audit_log",
    "写入审计日志。",
    {"type": "object", "properties": {"event_type": {"type": "string"}, "target_type": {"type": "string"}, "target_id": {"type": "string"}, "detail": {"type": "string"}}, "required": ["event_type", "detail"]},
)
async def write_audit_log(input: dict) -> dict[str, Any]:
    import aiosqlite
    from datetime import datetime, UTC
    db_path = _project_root() / "data" / "db" / "app.sqlite"
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
                db_path = _project_root() / "data" / "db" / "app.sqlite"
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
    db_path = _project_root() / "data" / "db" / "app.sqlite"
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
    fp = _project_root() / "data" / "memory" / input["path"]
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
    fp = _project_root() / "data" / "memory" / input["path"]
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
    db_path = _project_root() / "data" / "db" / "app.sqlite"
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

    db_path = _project_root() / "data" / "db" / "app.sqlite"
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
    "每次调用都会创建项目分析目录，并返回项目 Agent 输出、分析目录和 record-only session_id。",
    {"type": "object", "properties": {
        "project_name": {"type": "string", "description": "projects.yaml 中注册的项目名称"},
        "task": {"type": "string", "description": "要派发给项目 Agent 的任务描述（详细）"},
        "context": {"type": "string", "description": "消息的原始内容和背景信息，供项目 Agent 参考"},
        "skill": {"type": "string", "description": "可选：指定项目 Agent 必须加载的 workflow skill，例如 riot-log-triage"},
        "session_id": {"type": "string", "description": "可选兼容字段，仅审计记录；不会用于项目 Agent resume。不要传 workspace/input/session.json 里的全局 agent_session_id"},
        "db_session_id": {"type": "integer", "description": "DB 会话 ID，用于读取/持久化项目级 Agent session"},
        "message_id": {"type": "integer", "description": "可选：当前 DB 消息 ID，用于落盘目录 message-<id>"},
        "orchestration_turn_id": {"type": "string", "description": "可选：主编排本轮 ID，用于审计"},
    }, "required": ["project_name", "task"]},
)
async def dispatch_to_project(input: dict) -> dict[str, Any]:
    from core.orchestrator.agent_client import agent_client
    from core.app.project_workspace import (
        build_project_workspace_prompt_block,
        prepare_project_from_session_workspace_path,
    )
    from core.projects import get_project, merge_skills, prepare_project_runtime_context
    from core.skill_registry import SKILL_REGISTRY

    project_name = str(input["project_name"]).strip()
    task = str(input["task"])
    context = str(input.get("context") or "")
    requested_skill = str(input.get("skill") or "").strip()
    input_session_id_record = str(input.get("session_id") or "").strip()
    db_session_id = input.get("db_session_id")
    message_id = _extract_message_id(input)
    orchestration_turn_id = str(input.get("orchestration_turn_id") or "").strip()
    runtime = agent_client.get_active_runtime()

    project = get_project(project_name)
    if not project:
        return {"error": f"Project '{project_name}' not found. Call list_projects to see available projects."}

    import aiosqlite
    from datetime import datetime

    db_path = _app_db_path()
    analysis_workspace = ""
    session_agent_runtime = ""
    dispatch_run_id = None
    if db_session_id:
        try:
            async with aiosqlite.connect(str(db_path)) as db:
                cursor = await db.execute(
                    "SELECT analysis_mode, analysis_workspace, agent_runtime FROM sessions WHERE id = ?",
                    (db_session_id,),
                )
                row = await cursor.fetchone()
                if row:
                    analysis_workspace = str(row[1] or "")
                    session_agent_runtime = str(row[2] or "").strip()
        except Exception as e:
            logger.warning("Failed to read analysis session context: {}", e)
    if session_agent_runtime:
        runtime = normalize_agent_runtime(session_agent_runtime)

    async def _update_dispatch_run(
        *,
        status: str,
        input_path: str = "",
        output_path: str = "",
        error_message: str = "",
        cost_usd: float = 0.0,
    ) -> None:
        if not dispatch_run_id:
            return
        try:
            async with aiosqlite.connect(str(db_path)) as db:
                now = datetime.now().isoformat()
                await db.execute(
                    "UPDATE agent_runs SET status=?, ended_at=?, input_path=?, output_path=?, cost_usd=?, error_message=? WHERE id=?",
                    (status, now, input_path, output_path, cost_usd, error_message[:500], dispatch_run_id),
                )
                await db.commit()
        except Exception as exc:
            logger.warning("Failed to update dispatch AgentRun: {}", exc)

    async def _record_dispatch_paths(*, input_path: str = "", output_path: str = "") -> None:
        if not dispatch_run_id:
            return
        try:
            async with aiosqlite.connect(str(db_path)) as db:
                await db.execute(
                    "UPDATE agent_runs SET input_path=?, output_path=? WHERE id=?",
                    (input_path, output_path, dispatch_run_id),
                )
                await db.commit()
        except Exception as exc:
            logger.warning("Failed to record dispatch artifact paths: {}", exc)

    try:
        async with aiosqlite.connect(str(db_path)) as db:
            now = datetime.now().isoformat()
            cursor = await db.execute(
                "INSERT INTO agent_runs (agent_name, runtime_type, session_id, status, "
                "message_id, input_path, output_path, input_tokens, output_tokens, cost_usd, error_message, started_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"dispatch:{project_name}", get_agent_run_runtime_type(runtime), db_session_id, "running",
                 message_id, "", "", 0, 0, 0.0, "", now),
            )
            dispatch_run_id = cursor.lastrowid
            await db.commit()
    except Exception as e:
        logger.warning("Failed to create dispatch AgentRun: {}", e)

    dispatch_id = _dispatch_id(dispatch_run_id)
    runtime_context = None
    project_workspace: dict[str, Any] | None = None
    project_cwd = project.path
    session_workspace_path = None
    session_workspace: dict[str, Any] = {}
    artifact_roots: dict[str, str] = {}
    if db_session_id:
        session_workspace_path = _session_workspace_path(int(db_session_id))
        if session_workspace_path.exists():
            session_workspace = _read_json_artifact(session_workspace_path)
            roots = session_workspace.get("session_artifact_roots")
            if isinstance(roots, dict):
                artifact_roots = {str(key): str(value) for key, value in roots.items()}
            project_workspace_path = session_workspace_path.parent / "project_workspace.json"
            project_workspace = _read_json_artifact(project_workspace_path)
            project_entry = _project_entry_from_workspace(project_workspace, project_name)
            if _project_entry_is_ready(project_entry):
                project_cwd = Path(str(project_entry.get("worktree_path") or project_entry.get("execution_path")))
                if str(project_workspace.get("active_project") or "").strip() != project_name:
                    project_workspace["active_project"] = project_name
                    _write_json_artifact(project_workspace_path, project_workspace)

    selected_skill = ""
    auto_selected_skill = ""
    message_segment = _message_artifact_segment(message_id)
    project_segment = _safe_artifact_segment(project_name, fallback="project")
    if artifact_roots.get("session_dir"):
        session_dir = Path(artifact_roots["session_dir"])
    elif session_workspace_path:
        session_dir = session_workspace_path.parent
    else:
        session_dir = _sessions_root() / "no-session"
    analysis_root = Path(artifact_roots.get("analysis_dir") or (session_dir / ".analysis"))
    orchestration_root = Path(artifact_roots.get("orchestration_dir") or (session_dir / ".orchestration"))
    session_image_paths = _session_upload_image_paths(artifact_roots)
    analysis_dir = (
        analysis_root / message_segment / project_segment / dispatch_id
        if analysis_root
        else None
    )
    orchestration_dir = orchestration_root / message_segment if orchestration_root else None
    if analysis_dir:
        (analysis_dir / "artifacts").mkdir(parents=True, exist_ok=True)
    if orchestration_dir:
        orchestration_dir.mkdir(parents=True, exist_ok=True)

    project_entry = _project_entry_from_workspace(project_workspace, project_name)
    if (
        not requested_skill
        and _skill_registered(_RIOT_LOG_TRIAGE_SKILL)
        and _looks_like_riot_log_triage_task(
            project_name=project_name,
            task=task,
            context=context,
            artifact_roots=artifact_roots,
        )
    ):
        selected_skill = _RIOT_LOG_TRIAGE_SKILL
        auto_selected_skill = _RIOT_LOG_TRIAGE_SKILL

    triage_dir = _dispatch_triage_dir(
        analysis_workspace=analysis_workspace,
        artifact_roots=artifact_roots,
        requested_skill=requested_skill,
        selected_skill=selected_skill,
        message_segment=message_segment,
        project_segment=project_segment,
        dispatch_id=dispatch_id,
    )

    input_artifact = analysis_dir / "input.json" if analysis_dir else None
    prompt_artifact = analysis_dir / "prompt.md" if analysis_dir else None
    result_artifact = analysis_dir / "result.json" if analysis_dir else None
    trace_artifact = analysis_dir / "analysis_trace.md" if analysis_dir else None
    dispatch_artifact = orchestration_dir / f"{dispatch_id}.json" if orchestration_dir else None

    def _analysis_input_payload() -> dict[str, Any]:
        return {
            "message_id": message_id,
            "db_session_id": db_session_id,
            "orchestration_turn_id": orchestration_turn_id,
            "dispatch_id": dispatch_id,
            "project_name": project_name,
            "skill": selected_skill or requested_skill,
            "task": task,
            "context": context,
            "worktree_path": str(project_entry.get("worktree_path") or project_entry.get("execution_path") or project_cwd),
            "checkout_ref": str(project_entry.get("checkout_ref") or ""),
            "execution_commit_sha": str(project_entry.get("execution_commit_sha") or ""),
            "execution_version": str(project_entry.get("execution_version") or ""),
            "project_path": str(project_entry.get("project_path") or project_entry.get("source_path") or project.path),
            "input_session_id_record_only": input_session_id_record,
            "triage_dir": triage_dir,
            "image_paths": session_image_paths,
        }

    async def _fail_preflight(error_text: str, *, result_status: str) -> dict[str, Any]:
        _write_project_dispatch_failure(
            project_name=project_name,
            dispatch_id=dispatch_id,
            error_text=error_text,
            result_artifact=result_artifact,
            trace_artifact=trace_artifact,
            dispatch_artifact=dispatch_artifact,
            triage_dir=triage_dir,
            result_status=result_status,
        )
        await _update_dispatch_run(
            status="failed",
            input_path=str(input_artifact or ""),
            output_path=str(result_artifact or ""),
            error_message=error_text,
        )
        project_agent_run = _persist_project_run_record(
            session_workspace_path=session_workspace_path,
            project_name=project_name,
            message_id=message_id,
            dispatch_id=dispatch_id,
            skill=selected_skill,
            analysis_dir=analysis_dir,
            agent_session_id="",
            status=result_status,
        )
        return {
            "error": error_text,
            "project": project_name,
            "dispatch_id": dispatch_id,
            "agent_run_id": dispatch_run_id,
            "analysis_dir": str(analysis_dir) if analysis_dir else "",
            "result_path": str(result_artifact) if result_artifact else "",
            "triage_dir": triage_dir,
            "project_agent_run": project_agent_run,
        }

    if input_artifact:
        _write_json_artifact(input_artifact, _analysis_input_payload())
    if dispatch_artifact:
        _write_json_artifact(
            dispatch_artifact,
            {
                "dispatch_id": dispatch_id,
                "message_id": message_id,
                "db_session_id": db_session_id,
                "orchestration_turn_id": orchestration_turn_id,
                "project": project_name,
                "skill": selected_skill,
                "requested_skill": requested_skill,
                "auto_selected_skill": auto_selected_skill,
                "task_summary": task[:500],
                "context_summary": context[:1000],
                "analysis_dir": str(analysis_dir) if analysis_dir else "",
                "input_path": str(input_artifact or ""),
                "result_path": str(result_artifact or ""),
                "image_paths": session_image_paths,
                "status": "preflight",
            },
        )
    if trace_artifact:
        trace_artifact.write_text(
            "\n".join(
                [
                    "# Project Analysis Trace",
                    "",
                    "- status: preflight",
                    f"- project: {project_name}",
                    f"- dispatch_id: {dispatch_id}",
                    f"- skill: {selected_skill}",
                    f"- requested_skill: {requested_skill}",
                    f"- auto_selected_skill: {auto_selected_skill}",
                    f"- analysis_dir: {analysis_dir or ''}",
                    f"- triage_dir: {triage_dir}",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
    _ensure_triage_preflight(
        triage_dir=triage_dir,
        project_name=project_name,
        message_id=message_id,
        dispatch_id=dispatch_id,
        task=task,
        context=context,
        analysis_dir=analysis_dir,
        artifact_roots=artifact_roots,
    )
    await _record_dispatch_paths(input_path=str(input_artifact or ""), output_path=str(result_artifact or ""))

    if not _project_entry_is_ready(project_entry) and session_workspace_path and session_workspace_path.exists():
        try:
            entry = await asyncio.wait_for(
                asyncio.to_thread(
                    prepare_project_from_session_workspace_path,
                    project_name,
                    session_workspace_path=session_workspace_path,
                    reason="dispatch_to_project",
                    active=True,
                ),
                timeout=30,
            )
            if entry:
                project_cwd = Path(str(entry.get("worktree_path") or entry.get("execution_path") or project.path))
                project_workspace = _read_json_artifact(session_workspace_path.parent / "project_workspace.json")
                project_entry = _project_entry_from_workspace(project_workspace, project_name)
        except asyncio.TimeoutError:
            return await _fail_preflight(
                "dispatch_to_project preflight timed out while preparing project worktree after 30s",
                result_status="preflight_timeout",
            )
        except Exception as exc:
            return await _fail_preflight(
                f"dispatch_to_project preflight failed while preparing project worktree: {exc}",
                result_status="preflight_failed",
            )

    if not project_workspace:
        try:
            runtime_context = await asyncio.wait_for(
                asyncio.to_thread(prepare_project_runtime_context, project_name),
                timeout=30,
            )
        except asyncio.TimeoutError:
            return await _fail_preflight(
                "dispatch_to_project preflight timed out while preparing fallback project runtime after 30s",
                result_status="preflight_timeout",
            )
        except Exception as exc:
            return await _fail_preflight(
                f"dispatch_to_project preflight failed while preparing fallback project runtime: {exc}",
                result_status="preflight_failed",
            )
        if runtime_context:
            project_cwd = Path(runtime_context.execution_path)
            project_workspace = {
                "workspace_scope": "fallback",
                "active_project": project_name,
                "projects": {
                    project_name: runtime_context.to_payload(),
                },
            }
            project_entry = _project_entry_from_workspace(project_workspace, project_name)

    if input_artifact:
        _write_json_artifact(input_artifact, _analysis_input_payload())
    if dispatch_artifact:
        payload = _read_json_artifact(dispatch_artifact)
        payload.update(
            {
                "status": "preparing_prompt",
                "skill": selected_skill,
                "requested_skill": requested_skill,
                "auto_selected_skill": auto_selected_skill,
                "worktree_path": str(project_entry.get("worktree_path") or project_entry.get("execution_path") or project_cwd),
                "checkout_ref": str(project_entry.get("checkout_ref") or ""),
                "execution_commit_sha": str(project_entry.get("execution_commit_sha") or ""),
                "execution_version": str(project_entry.get("execution_version") or ""),
            }
        )
        _write_json_artifact(dispatch_artifact, payload)

    merged_agents = merge_skills(SKILL_REGISTRY, project_cwd, include_global=True)
    runtime_block = build_project_workspace_prompt_block(project_workspace)

    if requested_skill:
        if requested_skill in merged_agents:
            selected_skill = requested_skill
        else:
            error_text = f"Skill '{requested_skill}' not available for project '{project_name}'."
            if input_artifact:
                _write_json_artifact(input_artifact, _analysis_input_payload())
            if result_artifact:
                _write_json_artifact(
                    result_artifact,
                    {
                        "project": project_name,
                        "dispatch_id": dispatch_id,
                        "failed": True,
                        "error": error_text,
                        "project_agent_session_id": "",
                        "project_agent_session_record_only": True,
                        "triage_dir": triage_dir,
                        "structured_evidence_summary": [],
                    },
                )
            if trace_artifact:
                trace_artifact.write_text(
                    f"# Project Analysis Trace\n\n- status: failed\n- project: {project_name}\n- dispatch_id: {dispatch_id}\n- error: {error_text}\n",
                    encoding="utf-8",
                )
            if dispatch_artifact:
                _write_json_artifact(
                    dispatch_artifact,
                    {
                        "dispatch_id": dispatch_id,
                        "message_id": message_id,
                        "project": project_name,
                        "skill": requested_skill,
                        "task_summary": task[:500],
                        "context_summary": context[:1000],
                        "analysis_dir": str(analysis_dir) if analysis_dir else "",
                        "status": "failed",
                        "error": error_text,
                    },
                )
            await _update_dispatch_run(
                status="failed",
                input_path=str(input_artifact or ""),
                output_path=str(result_artifact or ""),
                error_message=error_text,
            )
            _persist_project_run_record(
                session_workspace_path=session_workspace_path,
                project_name=project_name,
                message_id=message_id,
                dispatch_id=dispatch_id,
                skill=requested_skill,
                analysis_dir=analysis_dir,
                agent_session_id="",
                status="failed",
            )
            return {
                "error": error_text,
                "project": project_name,
                "dispatch_id": dispatch_id,
                "analysis_dir": str(analysis_dir) if analysis_dir else "",
            }
    elif selected_skill and selected_skill not in merged_agents:
        logger.warning(
            "Auto-selected {} for dispatch {} project {}, but the skill is unavailable after merge; continuing without it",
            selected_skill,
            dispatch_id,
            project_name,
        )
        selected_skill = ""
        auto_selected_skill = ""
        triage_dir = ""

    if selected_skill:
        if auto_selected_skill:
            logger.info(
                "Auto-selected {} for dispatch {} project {}",
                _RIOT_LOG_TRIAGE_SKILL,
                dispatch_id,
                project_name,
            )
        merged_agents = {
            name: definition
            for name, definition in merged_agents.items()
            if name == selected_skill or name != "ones"
        }

    if input_artifact:
        _write_json_artifact(input_artifact, _analysis_input_payload())
    if dispatch_artifact:
        payload = _read_json_artifact(dispatch_artifact)
        payload.update(
            {
                "status": "prompt_ready",
                "skill": selected_skill,
                "requested_skill": requested_skill,
                "auto_selected_skill": auto_selected_skill,
                "triage_dir": triage_dir,
                "worktree_path": str(project_entry.get("worktree_path") or project_entry.get("execution_path") or project_cwd),
                "checkout_ref": str(project_entry.get("checkout_ref") or ""),
                "execution_commit_sha": str(project_entry.get("execution_commit_sha") or ""),
                "execution_version": str(project_entry.get("execution_version") or ""),
            }
        )
        _write_json_artifact(dispatch_artifact, payload)
    if trace_artifact:
        trace_artifact.write_text(
            "\n".join(
                [
                    "# Project Analysis Trace",
                    "",
                    "- status: prompt_ready",
                    f"- project: {project_name}",
                    f"- dispatch_id: {dispatch_id}",
                    f"- skill: {selected_skill}",
                    f"- requested_skill: {requested_skill}",
                    f"- auto_selected_skill: {auto_selected_skill}",
                    f"- analysis_dir: {analysis_dir or ''}",
                    f"- triage_dir: {triage_dir}",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

    project_only = list(merged_agents)
    skills_line = ""
    if project_only:
        skills_line = f"\n\n可用的项目 Skills: {', '.join(project_only)}"

    project_prompt = f"""你是项目 "{project.name}" 的工作 Agent。

源仓库目录: {project.path}
执行/分析目录: {project_cwd}
项目分析落盘目录: {analysis_dir or ""}
项目说明: {project.description}{skills_line}{runtime_block}

## 回复规则
{PROJECT_AGENT_RESPONSE_RULES}

## 指定 Skill
{f"必须使用 {selected_skill} workflow skill 完成本次项目任务。" if selected_skill else "未指定，按项目任务自行选择合适的项目 skill。"}

## 边界
你只负责当前项目 {project.name} 的一次性分析。不要判断或调度其他项目，不要调用 dispatch_to_project，不要生成最终飞书卡片，不要依赖历史项目 Agent session。
请把可追溯的分析摘要写入项目分析落盘目录；最终输出给主编排的是结构化结论和证据摘要。

## 任务
{task}"""

    if context:
        project_prompt += f"\n\n## 消息背景\n{context}"
    if triage_dir:
        project_prompt += f"\n\n## 关联 Triage 目录\n{triage_dir}"
    if session_image_paths:
        project_prompt += (
            "\n\n## 已作为视觉输入传入的图片\n"
            + "\n".join(f"- {path}" for path in session_image_paths)
        )

    project_prompt += (
        "\n\n请在执行/分析目录中处理以上任务，可以使用 Read、Bash、Glob、Grep 等工具访问项目文件。"
        "不要把源仓库目录作为工作目录；不要跨项目调度，缺少其他项目信息时返回给主编排说明需要补派。"
    )

    project_system = (
        f"你运行在项目 {project.name} 的 session worktree 中（{project_cwd}）。"
        f"源仓库目录仅供识别：{project.path}。使用项目级 skills 完成任务。"
        f"{f'本次必须使用 {selected_skill} workflow skill。' if selected_skill else ''}"
        f"项目分析落盘目录：{analysis_dir or ''}。"
        "你是一次性项目执行单元，不要 resume 历史项目 Agent session，不要调用 dispatch_to_project，不要生成最终飞书卡片。"
        f"{runtime_block}"
        f"\n\n{PROJECT_AGENT_RESPONSE_RULES}"
    )

    if input_artifact:
        _write_json_artifact(input_artifact, _analysis_input_payload())
    if prompt_artifact:
        prompt_artifact.write_text(project_prompt, encoding="utf-8")
    if dispatch_artifact:
        payload = _read_json_artifact(dispatch_artifact)
        payload.update(
            {
                "status": "running",
                "skill": selected_skill,
                "requested_skill": requested_skill,
                "auto_selected_skill": auto_selected_skill,
                "triage_dir": triage_dir,
                "prompt_path": str(prompt_artifact or ""),
                "worktree_path": str(project_entry.get("worktree_path") or project_entry.get("execution_path") or project_cwd),
                "checkout_ref": str(project_entry.get("checkout_ref") or ""),
                "execution_commit_sha": str(project_entry.get("execution_commit_sha") or ""),
                "execution_version": str(project_entry.get("execution_version") or ""),
            }
        )
        _write_json_artifact(dispatch_artifact, payload)

    project_dispatch_timeout = _project_dispatch_timeout_seconds()
    try:
        result = await asyncio.wait_for(
            agent_client.run_for_project(
                prompt=project_prompt,
                system_prompt=project_system,
                project_cwd=str(project_cwd),
                project_agents=merged_agents,
                max_turns=20,
                session_id=None,
                skill=selected_skill or None,
                runtime=runtime,
                image_paths=session_image_paths,
            ),
            timeout=project_dispatch_timeout,
        )
        new_session_id = str(result.get("session_id") or "").strip()
        if result.get("is_error"):
            error_text = str(result.get("text") or "project agent returned an error")
            if result_artifact:
                _write_json_artifact(
                    result_artifact,
                    {
                        "project": project_name,
                        "dispatch_id": dispatch_id,
                        "output_text": str(result.get("text") or ""),
                        "failed": True,
                        "error": error_text,
                        "usage": result.get("usage") if isinstance(result.get("usage"), dict) else {},
                        "cost_usd": result.get("cost_usd", 0),
                        "project_agent_session_id": new_session_id,
                        "project_agent_session_record_only": True,
                        "input_session_id_record_only": input_session_id_record,
                        "triage_dir": triage_dir,
                        "structured_evidence_summary": [],
                        "raw": result,
                    },
                )
            if trace_artifact:
                trace_artifact.write_text(
                    f"# Project Analysis Trace\n\n- status: failed\n- project: {project_name}\n- dispatch_id: {dispatch_id}\n- skill: {selected_skill or ''}\n- error: {error_text}\n",
                    encoding="utf-8",
                )
            if dispatch_artifact:
                payload = _read_json_artifact(dispatch_artifact)
                payload.update({"status": "failed", "error": error_text, "result_path": str(result_artifact or "")})
                _write_json_artifact(dispatch_artifact, payload)
            await _update_dispatch_run(
                status="failed",
                input_path=str(input_artifact or ""),
                output_path=str(result_artifact or ""),
                error_message=error_text,
                cost_usd=float(result.get("cost_usd") or 0),
            )
            project_agent_run = _persist_project_run_record(
                session_workspace_path=session_workspace_path,
                project_name=project_name,
                message_id=message_id,
                dispatch_id=dispatch_id,
                skill=selected_skill or "",
                analysis_dir=analysis_dir,
                agent_session_id=new_session_id,
                status="failed",
            )
            return {
                "error": error_text,
                "project": project_name,
                "session_id": new_session_id,
                "dispatch_id": dispatch_id,
                "analysis_dir": str(analysis_dir) if analysis_dir else "",
                "result_path": str(result_artifact) if result_artifact else "",
                "agent_session_scope": "record_only",
                "project_agent_run": project_agent_run,
            }

        if result_artifact:
            _write_json_artifact(
                result_artifact,
                {
                    "project": project_name,
                    "dispatch_id": dispatch_id,
                    "output_text": str(result.get("text") or ""),
                    "failed": False,
                    "error": "",
                    "usage": result.get("usage") if isinstance(result.get("usage"), dict) else {},
                    "cost_usd": result.get("cost_usd", 0),
                    "project_agent_session_id": new_session_id,
                    "project_agent_session_record_only": True,
                    "input_session_id_record_only": input_session_id_record,
                    "triage_dir": triage_dir,
                    "structured_evidence_summary": [],
                    "raw": result,
                },
            )
        if trace_artifact:
            trace_artifact.write_text(
                "\n".join(
                    [
                        "# Project Analysis Trace",
                        "",
                        f"- status: success",
                        f"- project: {project_name}",
                        f"- dispatch_id: {dispatch_id}",
                        f"- skill: {selected_skill or ''}",
                        f"- analysis_dir: {analysis_dir or ''}",
                        f"- triage_dir: {triage_dir}",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
        if dispatch_artifact:
            payload = _read_json_artifact(dispatch_artifact)
            payload.update(
                {
                    "status": "success",
                    "result_path": str(result_artifact or ""),
                    "project_agent_session_id": new_session_id,
                    "project_agent_session_record_only": True,
                }
            )
            _write_json_artifact(dispatch_artifact, payload)

        await _update_dispatch_run(
            status="success",
            input_path=str(input_artifact or ""),
            output_path=str(result_artifact or ""),
            cost_usd=float(result.get("cost_usd") or 0),
        )

        project_agent_run = _persist_project_run_record(
            session_workspace_path=session_workspace_path,
            project_name=project_name,
            message_id=message_id,
            dispatch_id=dispatch_id,
            skill=selected_skill or "",
            analysis_dir=analysis_dir,
            agent_session_id=new_session_id,
            status="success",
        )

        if db_session_id:
            try:
                async with aiosqlite.connect(str(db_path)) as db:
                    updates = ["project = ?", "agent_runtime = ?", "updated_at = ?"]
                    params = [project_name, runtime, datetime.now().isoformat()]
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
            "resume_session_id": None,
            "resume_source": "",
            "dispatch_id": dispatch_id,
            "agent_run_id": dispatch_run_id,
            "analysis_dir": str(analysis_dir) if analysis_dir else "",
            "result_path": str(result_artifact) if result_artifact else "",
            "triage_dir": triage_dir,
            "agent_session_scope": "record_only",
            "project_agent_run": project_agent_run,
        }
    except asyncio.TimeoutError:
        error_text = f"dispatch_to_project timed out after {project_dispatch_timeout}s"
        logger.warning("dispatch_to_project timed out for {} after {}s", project_name, project_dispatch_timeout)
        _write_project_dispatch_failure(
            project_name=project_name,
            dispatch_id=dispatch_id,
            error_text=error_text,
            result_artifact=result_artifact,
            trace_artifact=trace_artifact,
            dispatch_artifact=dispatch_artifact,
            triage_dir=triage_dir,
            result_status="timeout",
        )
        await _update_dispatch_run(
            status="failed",
            input_path=str(input_artifact or ""),
            output_path=str(result_artifact or ""),
            error_message=error_text,
        )
        project_agent_run = _persist_project_run_record(
            session_workspace_path=session_workspace_path,
            project_name=project_name,
            message_id=message_id,
            dispatch_id=dispatch_id,
            skill=selected_skill or "",
            analysis_dir=analysis_dir,
            agent_session_id="",
            status="timeout",
        )
        return {
            "error": error_text,
            "project": project_name,
            "dispatch_id": dispatch_id,
            "agent_run_id": dispatch_run_id,
            "analysis_dir": str(analysis_dir) if analysis_dir else "",
            "result_path": str(result_artifact) if result_artifact else "",
            "project_agent_run": project_agent_run,
        }
    except asyncio.CancelledError:
        error_text = "dispatch_to_project cancelled before project agent completed"
        logger.warning("dispatch_to_project cancelled for {}", project_name)
        _write_project_dispatch_failure(
            project_name=project_name,
            dispatch_id=dispatch_id,
            error_text=error_text,
            result_artifact=result_artifact,
            trace_artifact=trace_artifact,
            dispatch_artifact=dispatch_artifact,
            triage_dir=triage_dir,
            result_status="cancelled",
        )
        await _update_dispatch_run(
            status="failed",
            input_path=str(input_artifact or ""),
            output_path=str(result_artifact or ""),
            error_message=error_text,
        )
        _persist_project_run_record(
            session_workspace_path=session_workspace_path,
            project_name=project_name,
            message_id=message_id,
            dispatch_id=dispatch_id,
            skill=selected_skill or "",
            analysis_dir=analysis_dir,
            agent_session_id="",
            status="failed",
        )
        return {
            "error": error_text,
            "project": project_name,
            "dispatch_id": dispatch_id,
            "agent_run_id": dispatch_run_id,
            "analysis_dir": str(analysis_dir) if analysis_dir else "",
            "result_path": str(result_artifact) if result_artifact else "",
        }
    except Exception as e:
        logger.exception("dispatch_to_project failed for {}: {}", project_name, e)
        error_text = str(e)
        if result_artifact:
            _write_json_artifact(
                result_artifact,
                {
                    "project": project_name,
                    "dispatch_id": dispatch_id,
                    "failed": True,
                    "error": error_text,
                    "project_agent_session_id": "",
                    "project_agent_session_record_only": True,
                    "triage_dir": triage_dir,
                    "structured_evidence_summary": [],
                },
            )
        if trace_artifact:
            trace_artifact.write_text(
                f"# Project Analysis Trace\n\n- status: failed\n- project: {project_name}\n- dispatch_id: {dispatch_id}\n- error: {error_text}\n",
                encoding="utf-8",
            )
        if dispatch_artifact:
            payload = _read_json_artifact(dispatch_artifact)
            payload.update({"status": "failed", "error": error_text, "result_path": str(result_artifact or "")})
            _write_json_artifact(dispatch_artifact, payload)
        await _update_dispatch_run(
            status="failed",
            input_path=str(input_artifact or ""),
            output_path=str(result_artifact or ""),
            error_message=error_text,
        )
        _persist_project_run_record(
            session_workspace_path=session_workspace_path,
            project_name=project_name,
            message_id=message_id,
            dispatch_id=dispatch_id,
            skill=selected_skill or "",
            analysis_dir=analysis_dir,
            agent_session_id="",
            status="failed",
        )
        return {
            "error": error_text,
            "project": project_name,
            "dispatch_id": dispatch_id,
            "analysis_dir": str(analysis_dir) if analysis_dir else "",
            "result_path": str(result_artifact) if result_artifact else "",
        }


# ==================== MCP Server ====================

# Tools registered in MCP.
#
# Platform side effects such as sending Feishu messages, saving bot replies,
# updating sessions, and writing audit logs are intentionally not exposed to
# agents. MessageProcessor -> ResultHandler owns those effects through
# Repository and ChannelPort.
CUSTOM_TOOLS = [
    query_db,
    list_projects_tool,
    prepare_project_worktree_tool,
    dispatch_to_project,
]
if settings.enable_memory_tools:
    CUSTOM_TOOLS.extend([read_memory, search_memory_entries])

PROJECT_TOOLS = [
    query_db,
]
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

PROJECT_AGENT_RESPONSE_RULES = """
处理原则：
1. 默认先尽可能搜索代码、配置、脚本、注释和日志线索，再给用户结论。
2. 只有在你已经搜索过仍然缺少“会直接影响结论”的关键上下文时，才允许向用户追问。
3. 在没有至少完成一次仓库内检索（Read/Grep/Glob/Bash）之前，不允许直接向用户追问。
4. 如果必须追问，先简短说明你已经检查过什么，再只问最少必要的问题，通常 1 个，而且必须先给出已确认的部分结论。
5. 如果问题存在多层实现或多个可能口径，优先先给出你已经能确认的部分结论，再说明还缺哪一个关键信息。
6. 对 ONES / 现场问题，先做证据完整性检查。若缺少问题时间、相关日志/异常堆栈、业务 ID、配置截图/配置片段等关键证据，先要求最小补料，不要直接分析根因。
7. 对 ONES / 现场问题，现场证据只能来自当前工单、当前会话补料和仓库内能确认的代码/配置事实。不要把本机或仓库里其他无关历史日志当作现场证据；若引用，只能标注为实现参考。
8. 默认按纯静态分析处理：只读代码、配置、日志、历史记录和状态文件。除非用户明确要求“运行/构建/编译/测试/复现/启动服务”，禁止执行任何会启动项目、解析依赖或产生构建副作用的命令，例如 gradle/gradlew/mvn/npm/yarn/pnpm、docker compose、java -jar、pytest 等。需要验证时优先用已有日志和代码推理。
9. Bash 仅用于只读检索与轻量文件盘点，例如 rg、Get-Content、Get-ChildItem、git status/show/describe、压缩包目录查看；不要为了“顺手验证”运行项目构建、依赖解析、测试或服务启动。
10. 对工作类问题，优先输出一个“纯 JSON 对象”作为结构化回复，且不要包在代码块里。普通说明/分析优先用 `format=rich`；流程、步骤、链路、时序、状态流转、跨项目协作优先用 `format=flow`。
11. `format=rich` 示例：
   {"format":"rich","title":"标题","summary":"一句话总结","sections":[{"title":"结论","content":"核心结论"},{"title":"说明","content":"补充说明"}],"table":{"columns":[{"key":"item","label":"检查项","type":"text"}],"rows":[{"item":"配置"}]},"fallback_text":"纯文本兜底"}
12. `format=flow` 示例：
   {"format":"flow","title":"标题","summary":"一句话说明","steps":[{"title":"步骤1","detail":"说明"}],"table":{"columns":[{"key":"step","label":"步骤","type":"text"}],"rows":[{"step":"准备"}]},"mermaid":"flowchart TD\\nA[\\\"开始\\\"]-->B[\\\"结束\\\"]","fallback_text":"纯文本兜底"}
   流程图必须优先给 `mermaid` 或 `flowcharts[].source`，节点标签包含中文、空格、箭头、括号、斜杠、冒号或等号时必须加双引号，且不要把 Mermaid 放进 `code_blocks`。
13. 闲聊、问候、极短确认可以继续输出自然语言正文。
14. 不要输出 action/classified_type/topic/project_name/reply_content 等外层元字段。
""".strip()

# Orchestrator: separate MCP server with limited tools only.
# allowedTools does not restrict MCP tools, so we must register a
# dedicated server that only exposes the tools the orchestrator needs.
ORCHESTRATOR_TOOLS = [query_db, prepare_project_worktree_tool, dispatch_to_project]
if settings.enable_memory_tools:
    ORCHESTRATOR_TOOLS.extend([read_memory, search_memory_entries])

ORCHESTRATOR_MCP_SERVER = create_sdk_mcp_server(
    name="work-agent-tools",
    version="1.0.0",
    tools=ORCHESTRATOR_TOOLS,
)

ORCHESTRATOR_TOOL_NAMES = [
    f"mcp__work-agent-tools__{t.name}" for t in ORCHESTRATOR_TOOLS
]
