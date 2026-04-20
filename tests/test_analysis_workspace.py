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


@pytest.mark.asyncio
async def test_should_prepare_review_workspace_for_gitlab_issue_link(tmp_path, monkeypatch):
    import core.pipeline as pipeline_mod

    monkeypatch.setattr(pipeline_mod, "_triage_base_dir", lambda: tmp_path / ".triage")
    monkeypatch.setattr(pipeline_mod, "_review_base_dir", lambda: tmp_path / ".review")

    msg = {
        "content": "http://git.standard-robots.com/cybertron/allspark/-/issues/1072",
        "media_info_json": json.dumps({}, ensure_ascii=False),
    }
    assert await pipeline_mod._should_prepare_analysis_workspace(msg, session=None, session_id=3) is True


def test_analysis_dir_for_review_message_uses_review_base(tmp_path, monkeypatch):
    import core.pipeline as pipeline_mod

    monkeypatch.setattr(pipeline_mod, "_triage_base_dir", lambda: tmp_path / ".triage")
    monkeypatch.setattr(pipeline_mod, "_review_base_dir", lambda: tmp_path / ".review")

    review_dir = pipeline_mod._analysis_dir_for_session(
        9,
        session=None,
        msg={"content": "http://git.standard-robots.com/cybertron/allspark/-/issues/1072"},
    )

    assert review_dir.parent == tmp_path / ".review"
    assert review_dir.name.startswith("session-9-")


def test_analysis_requested_for_image_question():
    import core.pipeline as pipeline_mod

    msg = {
        "content": "这个图片内容是什么",
        "media_info_json": json.dumps({"type": "post", "has_image": True}, ensure_ascii=False),
    }
    assert pipeline_mod._analysis_requested(msg) is True


def test_should_capture_process_trace_for_ones_and_urgent_issue():
    import core.pipeline as pipeline_mod

    ones_msg = {
        "content": "请看这个 https://ones.standard-robots.com:10120/project/#/team/UNrQ5Ny5/task/1FmsdpJjHT3JPyWL",
    }
    assert pipeline_mod._should_capture_process_trace(
        ones_msg,
        session=None,
        parsed={"classified_type": "chat"},
    ) is True

    ordinary_msg = {"content": "收到"}
    assert pipeline_mod._should_capture_process_trace(
        ordinary_msg,
        session=None,
        parsed={"classified_type": "urgent_issue"},
    ) is True


def test_summarize_codex_rollout_collects_commentary_and_tool_steps(tmp_path):
    import core.pipeline as pipeline_mod

    rollout_path = tmp_path / "rollout.jsonl"
    rollout_path.write_text("", encoding="utf-8")
    events = [
        {
            "timestamp": "2026-04-16T05:50:14Z",
            "type": "event_msg",
            "payload": {
                "type": "agent_message",
                "phase": "commentary",
                "message": "先抓 ONES 工单。",
            },
        },
        {
            "timestamp": "2026-04-16T05:51:21Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "call_id": "call_1",
                "name": "shell_command",
                "arguments": json.dumps({"command": "python ones_cli.py fetch-task 'link'"}, ensure_ascii=False),
            },
        },
        {
            "timestamp": "2026-04-16T05:51:27Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": "Exit code: 0\nOutput:\nfetch ok\n",
            },
        },
    ]

    summary = pipeline_mod._summarize_codex_rollout(rollout_path, events)

    assert summary["runtime"] == "codex"
    assert summary["rollout_path"] == str(rollout_path)
    assert len(summary["steps"]) == 3
    assert summary["steps"][0]["kind"] == "commentary"
    assert summary["steps"][1]["kind"] == "tool_call"
    assert "fetch-task" in summary["steps"][1]["detail"]
    assert summary["steps"][2]["kind"] == "tool_output"


@pytest.mark.asyncio
async def test_fetch_ones_task_artifacts_uses_existing_task_dir(tmp_path, monkeypatch):
    import core.pipeline as pipeline_mod

    ones_root = tmp_path / ".ones" / "124548_jHLdrkhKn19k2SMU"
    ones_root.mkdir(parents=True, exist_ok=True)
    task_json = ones_root / "task.json"
    task_json.write_text(json.dumps({
        "task": {"uuid": "jHLdrkhKn19k2SMU", "number": 124548, "summary": "demo"},
        "paths": {"task_dir": str(ones_root), "task_json": str(task_json)},
    }, ensure_ascii=False), encoding="utf-8")

    monkeypatch.setattr(pipeline_mod, "settings", SimpleNamespace(project_root=tmp_path))

    msg = {
        "content": "https://ones.standard-robots.com:10120/project/#/team/UNrQ5Ny5/task/jHLdrkhKn19k2SMU",
    }

    payload = await pipeline_mod._fetch_ones_task_artifacts(msg)

    assert payload is not None
    assert payload["task"]["uuid"] == "jHLdrkhKn19k2SMU"
    assert payload["paths"]["task_dir"] == str(ones_root)


def test_build_ones_artifact_context_includes_downloaded_files(tmp_path):
    import core.pipeline as pipeline_mod

    messages_json = tmp_path / "messages.json"
    messages_json.write_text(json.dumps({
        "description_images": [{"label": "desc-img", "path": "D:/tmp/desc.png", "uuid": "img-1"}],
        "attachment_downloads": [{"label": "detail_log.zip", "path": "D:/tmp/detail_log.zip", "uuid": "att-1"}],
    }, ensure_ascii=False), encoding="utf-8")

    ones_result = {
        "task": {"number": 124548, "uuid": "jHLdrkhKn19k2SMU", "summary": "demo"},
        "project": {"display_name": "东莞台达七厂东莞SMT车间项目", "confidence": "high"},
        "counts": {"downloaded_message_attachments": 1},
        "paths": {
            "task_dir": "D:/tmp/.ones/124548_jHLdrkhKn19k2SMU",
            "task_json": "D:/tmp/.ones/124548_jHLdrkhKn19k2SMU/task.json",
            "messages_json": str(messages_json),
            "report_md": "D:/tmp/.ones/124548_jHLdrkhKn19k2SMU/report.md",
        },
    }

    context = pipeline_mod._build_ones_artifact_context(ones_result)

    assert "[ONES 下载产物]" in context
    assert "detail_log.zip" in context
    assert "messages.json" in context


def test_evaluate_ones_artifact_completeness_flags_missing_logs():
    import core.pipeline as pipeline_mod

    ones_result = {
        "task": {
            "uuid": "task-demo-1",
            "summary": "订单冻结",
            "description": "发生问题时间：2026-04-07 11:00，订单 order-123 一直卡住，11号车无法继续执行。",
        },
        "named_fields": {
            "项目名称": "allspark",
            "车辆序列号": "2410084",
        },
    }

    check = pipeline_mod._evaluate_ones_artifact_completeness(
        {"content": "帮我分析这个 ONES"},
        ones_result,
        analysis_dir=None,
    )

    assert check["status"] == "partial"
    assert "相关日志/异常堆栈" in check["missing_items"]


def test_evaluate_ones_artifact_completeness_accepts_complete_minimum_evidence(tmp_path):
    import core.pipeline as pipeline_mod

    messages_json = tmp_path / "messages.json"
    messages_json.write_text(json.dumps({
        "attachment_downloads": [
            {
                "label": "all_log.zip",
                "path": "D:/tmp/all_log.zip",
                "uuid": "att-1",
            }
        ],
    }, ensure_ascii=False), encoding="utf-8")

    ones_result = {
        "task": {
            "uuid": "task-demo-2",
            "summary": "自动充电任务未生成",
            "description": "发生问题时间：2026-04-07 11:00，11号车在停靠点无法生成自动充电任务。",
        },
        "named_fields": {
            "车辆序列号": "2410084",
            "项目名称": "allspark",
        },
        "paths": {
            "messages_json": str(messages_json),
        },
    }

    check = pipeline_mod._evaluate_ones_artifact_completeness(
        {"content": "继续分析这个 ONES"},
        ones_result,
        analysis_dir=None,
    )

    assert check["status"] == "complete"
    assert check["missing_items"] == []


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
async def test_materialize_review_workspace_uses_review_directory(tmp_path, monkeypatch):
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
                1, "feishu", "om_review_001", "oc_x", "ou_x", "tester",
                "text", "http://git.standard-robots.com/cybertron/allspark/-/issues/1072", now, "",
                json.dumps({}, ensure_ascii=False),
                "", "", "", "", None, 1, "pending", "", None, now,
            ),
        )
        await db.commit()

    monkeypatch.setattr(pipeline_mod, "DB_PATH", str(db_file))
    monkeypatch.setattr(pipeline_mod, "_triage_base_dir", lambda: tmp_path / ".triage")
    monkeypatch.setattr(pipeline_mod, "_review_base_dir", lambda: tmp_path / ".review")

    review_dir = await pipeline_mod._materialize_analysis_workspace(
        1,
        {"id": 1, "content": "http://git.standard-robots.com/cybertron/allspark/-/issues/1072", "platform_message_id": "om_review_001"},
        {"id": 1, "title": "Issue 1072 review", "topic": "", "project": "allspark"},
        1,
    )

    assert review_dir.exists()
    assert review_dir.parent == tmp_path / ".review"
    assert (review_dir / "00-state.json").exists()


@pytest.mark.asyncio
async def test_download_message_media_to_workspace_for_post_images(tmp_path):
    import core.pipeline as pipeline_mod

    fake_client = SimpleNamespace(
        get_image_bytes=lambda image_key, *args, **kwargs: (b"\x89PNGpost-image", "post.png"),
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
