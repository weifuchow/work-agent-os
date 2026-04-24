from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest


_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT DEFAULT 'feishu',
    platform_message_id TEXT UNIQUE,
    chat_id TEXT,
    sender_id TEXT,
    sender_name TEXT DEFAULT '',
    message_type TEXT DEFAULT 'text',
    content TEXT,
    received_at TEXT,
    raw_payload TEXT DEFAULT '',
    media_info_json TEXT DEFAULT '',
    attachment_path TEXT DEFAULT '',
    thread_id TEXT DEFAULT '',
    root_id TEXT DEFAULT '',
    parent_id TEXT DEFAULT '',
    classified_type TEXT,
    session_id INTEGER,
    pipeline_status TEXT DEFAULT 'pending',
    pipeline_error TEXT DEFAULT '',
    processed_at TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_key TEXT UNIQUE,
    source_platform TEXT DEFAULT 'feishu',
    source_chat_id TEXT,
    owner_user_id TEXT,
    title TEXT DEFAULT '',
    topic TEXT DEFAULT '',
    project TEXT DEFAULT '',
    priority TEXT DEFAULT 'normal',
    status TEXT DEFAULT 'open',
    task_context_id INTEGER,
    thread_id TEXT DEFAULT '',
    agent_session_id TEXT DEFAULT '',
    agent_runtime TEXT DEFAULT 'claude',
    analysis_mode INTEGER DEFAULT 0,
    analysis_workspace TEXT DEFAULT '',
    summary_path TEXT DEFAULT '',
    last_active_at TEXT,
    message_count INTEGER DEFAULT 0,
    risk_level TEXT DEFAULT 'low',
    needs_manual_review INTEGER DEFAULT 0,
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS session_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER,
    message_id INTEGER,
    role TEXT,
    sequence_no INTEGER,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT,
    target_type TEXT,
    target_id TEXT,
    detail TEXT,
    operator TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS agent_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER,
    agent_name TEXT,
    runtime_type TEXT,
    session_id INTEGER,
    status TEXT,
    started_at TEXT,
    ended_at TEXT,
    cost_usd REAL DEFAULT 0,
    input_path TEXT DEFAULT '',
    output_path TEXT DEFAULT '',
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    error_message TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS task_contexts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT,
    description TEXT DEFAULT '',
    status TEXT DEFAULT 'active',
    created_at TEXT,
    updated_at TEXT
);
"""


async def _insert_session(db_path: str, *, thread_id: str, project: str) -> int:
    now = datetime.now().isoformat()
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "INSERT INTO sessions (session_key, source_platform, source_chat_id, owner_user_id, title, topic, "
            "project, priority, status, thread_id, agent_runtime, analysis_mode, analysis_workspace, summary_path, "
            "last_active_at, message_count, risk_level, needs_manual_review, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "session_attach_001",
                "feishu",
                "oc_attach_001",
                "ou_attach_001",
                "附件分析",
                "",
                project,
                "normal",
                "open",
                thread_id,
                "claude",
                0,
                "",
                "",
                now,
                0,
                "low",
                0,
                now,
                now,
            ),
        )
        await db.commit()
        return cursor.lastrowid


async def _insert_message(
    db_path: str,
    *,
    session_id: int,
    platform_message_id: str,
    content: str,
    message_type: str,
    media_info: dict | None = None,
    thread_id: str = "",
) -> int:
    now = datetime.now().isoformat()
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "INSERT INTO messages (platform, platform_message_id, chat_id, sender_id, sender_name, message_type, "
            "content, received_at, raw_payload, media_info_json, attachment_path, thread_id, root_id, parent_id, "
            "classified_type, session_id, pipeline_status, pipeline_error, processed_at, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "feishu",
                platform_message_id,
                "oc_attach_001",
                "ou_attach_001",
                "tester",
                message_type,
                content,
                now,
                "",
                json.dumps(media_info or {}, ensure_ascii=False),
                "",
                thread_id,
                "",
                "",
                None,
                session_id,
                "pending",
                "",
                None,
                now,
            ),
        )
        await db.commit()
        return cursor.lastrowid


async def _read_row(db_path: str, table: str, row_id: int) -> dict:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,))
        row = await cursor.fetchone()
        assert row is not None
        return dict(row)


@pytest.mark.asyncio
async def test_media_only_message_is_staged_before_later_text_analysis(tmp_path, monkeypatch):
    import core.pipeline as pipeline_mod

    db_path = str(tmp_path / "app.sqlite")
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(_SCHEMA)
        await db.commit()

    thread_id = "thread_attach_001"
    session_id = await _insert_session(db_path, thread_id=thread_id, project="allspark")

    media_msg_id = await _insert_message(
        db_path,
        session_id=session_id,
        platform_message_id="om_attach_img_001",
        content="[图片]",
        message_type="image",
        media_info={"type": "image", "image_key": "img_attach_001"},
        thread_id=thread_id,
    )

    replies: list[dict] = []
    image_download_calls = {"count": 0}

    client = MagicMock()

    def _reply_message(message_id, content, reply_in_thread=True, msg_type="text"):
        replies.append({
            "message_id": message_id,
            "content": content,
            "msg_type": msg_type,
        })
        return {"message_id": f"bot_{message_id}", "thread_id": thread_id}

    def _get_image_bytes(*args, **kwargs):
        image_download_calls["count"] += 1
        return b"\x89PNGstage-flow", "capture.png"

    client.reply_message.side_effect = _reply_message
    client.get_image_bytes.side_effect = _get_image_bytes
    client.get_file_bytes.return_value = None

    temp_root = tmp_path / "allspark" / ".temp_resource"
    triage_root = tmp_path / ".triage"

    monkeypatch.setattr(pipeline_mod, "DB_PATH", db_path)
    monkeypatch.setattr(pipeline_mod, "_resolve_temp_resource_root", lambda session: temp_root)
    monkeypatch.setattr(pipeline_mod, "_triage_base_dir", lambda: triage_root)

    with patch("core.connectors.feishu.FeishuClient", return_value=client), \
         patch("core.pipeline.get_agent_runtime_override", return_value=None), \
         patch("core.pipeline._should_capture_process_trace", return_value=False), \
         patch("core.pipeline._run_project_agent", AsyncMock()) as mock_project, \
         patch("core.pipeline._run_orchestrator", AsyncMock()) as mock_orch:
        await pipeline_mod.process_message(media_msg_id)

        media_row = await _read_row(db_path, "messages", media_msg_id)
        media_info = json.loads(media_row["media_info_json"])
        staged_path = Path(media_row["attachment_path"])

        assert staged_path.exists()
        assert staged_path.read_bytes() == b"\x89PNGstage-flow"
        assert media_info["temp_resource_dir"] == str(staged_path.parent.parent.resolve())
        assert "已存储到" in replies[0]["content"]
        assert image_download_calls["count"] == 1
        assert mock_project.await_count == 0
        assert mock_orch.await_count == 0

        text_msg_id = await _insert_message(
            db_path,
            session_id=session_id,
            platform_message_id="om_attach_txt_002",
            content="帮我分析刚才的图片",
            message_type="text",
            media_info={},
            thread_id=thread_id,
        )

        with patch("core.projects.prepare_project_runtime_context", return_value=None), \
             patch("core.pipeline._run_project_agent", AsyncMock(return_value={
                 "text": "分析完成",
                 "session_id": "sdk-proj-attach-001",
                 "cost_usd": 0.01,
             })) as mock_project_text, \
             patch("core.pipeline._run_orchestrator", AsyncMock()) as mock_orch_text:
            await pipeline_mod.process_message(text_msg_id)

        session_row = await _read_row(db_path, "sessions", session_id)
        assert session_row["analysis_workspace"]
        copied_path = Path(session_row["analysis_workspace"]) / "01-intake" / "attachments" / "om_attach_img_001" / "original" / "capture.png"
        assert copied_path.exists()
        assert copied_path.read_bytes() == b"\x89PNGstage-flow"
        assert image_download_calls["count"] == 1
        assert mock_project_text.await_count == 1
        passed_image_paths = mock_project_text.await_args.kwargs["image_paths"]
        assert passed_image_paths
        assert str(copied_path.resolve()) in passed_image_paths or str(staged_path.resolve()) in passed_image_paths
        assert mock_orch_text.await_count == 0
        assert len(replies) == 2


@pytest.mark.asyncio
async def test_non_project_followup_question_receives_recent_session_image(tmp_path, monkeypatch):
    import core.pipeline as pipeline_mod

    db_path = str(tmp_path / "app.sqlite")
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(_SCHEMA)
        await db.commit()

    thread_id = "thread_attach_002"
    session_id = await _insert_session(db_path, thread_id=thread_id, project="")

    media_msg_id = await _insert_message(
        db_path,
        session_id=session_id,
        platform_message_id="om_attach_img_101",
        content="[图片]",
        message_type="image",
        media_info={"type": "image", "image_key": "img_attach_101"},
        thread_id=thread_id,
    )

    replies: list[dict] = []
    client = MagicMock()

    def _reply_message(message_id, content, reply_in_thread=True, msg_type="text"):
        replies.append({
            "message_id": message_id,
            "content": content,
            "msg_type": msg_type,
        })
        return {"message_id": f"bot_{message_id}", "thread_id": thread_id}

    client.reply_message.side_effect = _reply_message
    client.get_image_bytes.return_value = (b"\x89PNGstage-orch", "capture.png")
    client.get_file_bytes.return_value = None

    monkeypatch.setattr(pipeline_mod, "DB_PATH", db_path)
    monkeypatch.setattr(
        pipeline_mod,
        "_resolve_temp_resource_root",
        lambda session: tmp_path / ".temp_resource",
    )

    with patch("core.connectors.feishu.FeishuClient", return_value=client), \
         patch("core.pipeline.get_agent_runtime_override", return_value=None), \
         patch("core.pipeline._should_capture_process_trace", return_value=False), \
         patch("core.pipeline._run_project_agent", AsyncMock()) as mock_project, \
         patch("core.pipeline._run_orchestrator", AsyncMock(return_value={
             "text": "这是一个物体",
             "session_id": "sdk-orch-attach-001",
             "cost_usd": 0.01,
         })) as mock_orch:
        await pipeline_mod.process_message(media_msg_id)

        text_msg_id = await _insert_message(
            db_path,
            session_id=session_id,
            platform_message_id="om_attach_txt_102",
            content="这是什么",
            message_type="text",
            media_info={},
            thread_id=thread_id,
        )
        await pipeline_mod.process_message(text_msg_id)

    assert mock_project.await_count == 0
    assert mock_orch.await_count == 1
    passed_image_paths = mock_orch.await_args.kwargs["image_paths"]
    assert passed_image_paths
    assert any(Path(path).exists() for path in passed_image_paths)
    assert len(replies) == 2
