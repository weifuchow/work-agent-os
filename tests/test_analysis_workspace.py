from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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
"""


@pytest.mark.asyncio
async def test_should_prepare_analysis_workspace_requires_explicit_analysis(tmp_path, monkeypatch):
    import core.pipeline as pipeline_mod

    monkeypatch.setattr(pipeline_mod, "_triage_base_dir", lambda: tmp_path / ".triage")

    msg = {"content": "收到附件了", "media_info_json": json.dumps({"type": "image"})}
    assert await pipeline_mod._should_prepare_analysis_workspace(msg, session=None, session_id=1) is False

    analysis_msg = {"content": "帮我分析这个问题", "media_info_json": json.dumps({"type": "image"})}
    assert await pipeline_mod._should_prepare_analysis_workspace(analysis_msg, session=None, session_id=1) is True

    existing_dir = (tmp_path / ".triage" / "session-1-demo")
    existing_dir.mkdir(parents=True, exist_ok=True)
    assert await pipeline_mod._should_prepare_analysis_workspace(msg, session=None, session_id=1) is True


@pytest.mark.asyncio
async def test_should_prepare_analysis_workspace_respects_session_level_analysis_mode(tmp_path, monkeypatch):
    import core.pipeline as pipeline_mod

    monkeypatch.setattr(pipeline_mod, "_triage_base_dir", lambda: tmp_path / ".triage")

    msg = {"content": "收到你说的那个截图了", "media_info_json": json.dumps({"type": "image"})}
    session = {"analysis_mode": 1, "analysis_workspace": str(tmp_path / ".triage" / "session-2-case")}

    assert await pipeline_mod._should_prepare_analysis_workspace(msg, session=session, session_id=2) is True


def test_analysis_requested_for_image_question():
    import core.pipeline as pipeline_mod

    msg = {
        "content": "这个图片内容是什么",
        "media_info_json": json.dumps({"type": "post", "has_image": True}, ensure_ascii=False),
    }
    assert pipeline_mod._analysis_requested(msg) is True


@pytest.mark.asyncio
async def test_materialize_analysis_workspace_downloads_related_session_media(tmp_path, monkeypatch):
    import core.pipeline as pipeline_mod

    db_file = tmp_path / "app.sqlite"
    async with aiosqlite.connect(db_file) as db:
        await db.executescript(_SCHEMA)
        now = datetime.now().isoformat()
        await db.execute(
            "INSERT INTO messages (id, platform, platform_message_id, chat_id, sender_id, sender_name, "
            "message_type, content, received_at, raw_payload, media_info_json, attachment_path, thread_id, root_id, parent_id, "
            "classified_type, session_id, pipeline_status, pipeline_error, processed_at, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                1, "feishu", "om_img_001", "oc_x", "ou_x", "tester",
                "image", "[图片]", now, "",
                json.dumps({"type": "image", "image_key": "img_key_001"}, ensure_ascii=False),
                "", "", "", "", None, 1, "pending", "", None, now,
            ),
        )
        await db.execute(
            "INSERT INTO messages (id, platform, platform_message_id, chat_id, sender_id, sender_name, "
            "message_type, content, received_at, raw_payload, media_info_json, attachment_path, thread_id, root_id, parent_id, "
            "classified_type, session_id, pipeline_status, pipeline_error, processed_at, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                2, "feishu", "om_txt_002", "oc_x", "ou_x", "tester",
                "text", "帮我分析这个问题", now, "",
                json.dumps({}, ensure_ascii=False),
                "", "", "", "", None, 1, "pending", "", None, now,
            ),
        )
        await db.commit()

    monkeypatch.setattr(pipeline_mod, "DB_PATH", str(db_file))
    monkeypatch.setattr(pipeline_mod, "_triage_base_dir", lambda: tmp_path / ".triage")

    fake_client = SimpleNamespace(
        get_image_bytes=lambda image_key: (b"\x89PNGtriage-image", "capture.png"),
        get_file_bytes=lambda file_key: None,
    )

    with patch("core.connectors.feishu.FeishuClient", return_value=fake_client):
        triage_dir = await pipeline_mod._materialize_analysis_workspace(
            2,
            {"id": 2, "content": "帮我分析这个问题", "platform_message_id": "om_txt_002"},
            {"id": 1, "title": "图片问题分析", "topic": "", "project": "allspark"},
            1,
        )

    assert triage_dir.exists()
    assert (triage_dir / "00-state.json").exists()
    assert (triage_dir / "01-intake" / "messages" / "1.json").exists()
    assert (triage_dir / "01-intake" / "messages" / "2.json").exists()

    attachment_dir = triage_dir / "01-intake" / "attachments" / "om_img_001" / "original"
    files = list(attachment_dir.iterdir())
    assert len(files) == 1
    assert files[0].read_bytes() == b"\x89PNGtriage-image"

    refreshed = await pipeline_mod._read_message(1)
    assert refreshed is not None
    media_info = json.loads(refreshed["media_info_json"])
    assert media_info["download_status"] == "downloaded"
    assert refreshed["attachment_path"] == str(files[0].resolve())


@pytest.mark.asyncio
async def test_download_message_media_to_workspace_for_post_images(tmp_path):
    import core.pipeline as pipeline_mod

    fake_client = SimpleNamespace(
        get_image_bytes=lambda image_key: (b"\x89PNGpost-image", "post.png"),
        get_file_bytes=lambda *args, **kwargs: None,
    )

    msg = {
        "id": 5,
        "platform_message_id": "om_post_img_001",
        "message_type": "post",
        "media_info_json": json.dumps({
            "type": "post",
            "image_keys": ["img_v3_001"],
            "has_image": True,
        }, ensure_ascii=False),
    }

    with patch("core.connectors.feishu.FeishuClient", return_value=fake_client):
        media_info = await pipeline_mod._download_message_media_to_workspace(msg, tmp_path / ".triage" / "case-1")

    assert media_info["download_status"] == "downloaded"
    assert media_info["local_paths"]
    stored_path = Path(media_info["local_paths"][0])
    assert stored_path.exists()
    assert stored_path.read_bytes() == b"\x89PNGpost-image"
