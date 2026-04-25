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


def test_extract_vehicle_name_matches_chinese_adjacent_token():
    import core.pipeline as pipeline_mod

    assert pipeline_mod._extract_vehicle_name_from_text("车辆AG0019在电梯内不出来") == "AG0019"


@pytest.mark.asyncio
async def test_should_prepare_analysis_workspace_requires_explicit_analysis(tmp_path, monkeypatch):
    import core.pipeline as pipeline_mod

    monkeypatch.setattr(pipeline_mod, "_triage_base_dir", lambda: tmp_path / ".triage")

    msg = {"content": "[图片]", "media_info_json": json.dumps({"type": "image"})}
    assert await pipeline_mod._should_prepare_analysis_workspace(msg, session=None, session_id=1) is False

    analysis_msg = {"content": "帮我分析这个问题", "media_info_json": json.dumps({"type": "image"})}
    assert await pipeline_mod._should_prepare_analysis_workspace(analysis_msg, session=None, session_id=1) is True

    existing_dir = (tmp_path / ".triage" / "session-1-demo")
    existing_dir.mkdir(parents=True, exist_ok=True)
    assert await pipeline_mod._should_prepare_analysis_workspace(msg, session=None, session_id=1) is False

    text_msg = {"content": "继续分析刚才的附件", "media_info_json": json.dumps({}, ensure_ascii=False)}
    assert await pipeline_mod._should_prepare_analysis_workspace(text_msg, session=None, session_id=1) is True


@pytest.mark.asyncio
async def test_should_prepare_analysis_workspace_respects_session_level_analysis_mode(tmp_path, monkeypatch):
    import core.pipeline as pipeline_mod

    monkeypatch.setattr(pipeline_mod, "_triage_base_dir", lambda: tmp_path / ".triage")

    msg = {"content": "收到你说的那个截图了", "media_info_json": json.dumps({"type": "image"})}
    session = {"analysis_mode": 1, "analysis_workspace": str(tmp_path / ".triage" / "session-2-case")}

    assert await pipeline_mod._should_prepare_analysis_workspace(msg, session=session, session_id=2) is True

    media_only_msg = {"content": "[图片]", "media_info_json": json.dumps({"type": "image"}, ensure_ascii=False)}
    assert await pipeline_mod._should_prepare_analysis_workspace(media_only_msg, session=session, session_id=2) is False


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


def test_resolve_riot_log_triage_strategy_prefers_saved_triage_session(tmp_path):
    import core.pipeline as pipeline_mod

    strategy = pipeline_mod._resolve_riot_log_triage_strategy(
        project_name="allspark",
        analysis_dir=tmp_path / ".triage" / "case-1",
        session={"analysis_mode": 1, "analysis_workspace": str(tmp_path / ".triage" / "case-1")},
        triage_state={
            "mode": "structured",
            "agent_context": {
                "session_id": "triage-session-001",
                "runtime": "codex",
                "skill": "riot-log-triage",
            },
        },
        ones_artifacts={"task": {"number": 1}},
        msg={"content": "继续分析这单"},
        agent_runtime="codex",
    )

    assert strategy["enabled"] is True
    assert strategy["session_id"] == "triage-session-001"
    assert strategy["preferred_skill"] == "riot-log-triage"
    assert strategy["exclude_skills"] == {"ones"}
    assert strategy["route_mode"] == "triage_resume"


def test_resolve_riot_log_triage_strategy_starts_fresh_on_correction_turn(tmp_path):
    import core.pipeline as pipeline_mod

    strategy = pipeline_mod._resolve_riot_log_triage_strategy(
        project_name="allspark",
        analysis_dir=tmp_path / ".triage" / "case-1",
        session={"analysis_mode": 1, "analysis_workspace": str(tmp_path / ".triage" / "case-1")},
        triage_state={
            "mode": "structured",
            "agent_context": {
                "session_id": "triage-session-001",
                "runtime": "codex",
                "skill": "riot-log-triage",
            },
        },
        ones_artifacts=None,
        msg={"content": "CrossMapManager日志打印出来，http是干扰项"},
        agent_runtime="codex",
    )

    assert strategy["enabled"] is True
    assert strategy["session_id"] == ""
    assert strategy["route_mode"] == "triage_project"
    assert strategy["correction_turn"] is True


def test_ensure_riot_log_triage_state_ready_seeds_time_alignment_and_keyword_package(tmp_path):
    import core.pipeline as pipeline_mod

    analysis_dir = tmp_path / ".triage" / "case-1"
    analysis_dir.mkdir(parents=True, exist_ok=True)
    state = pipeline_mod._triage_state_payload(
        session_id=88,
        session={"project": "allspark", "title": "AG0019 乘梯后卡住"},
        msg={"content": "帮我分析 AG0019 乘梯卡住"},
        triage_dir=analysis_dir,
    )
    pipeline_mod._save_triage_state(analysis_dir, state)

    ones_result = {
        "summary_snapshot": {
            "status": "ready",
            "summary_text": "AG0019 在 2026-04-21 18:34 乘梯从 4 楼去 5 楼后长时间不动。",
            "problem_time": "2026-04-21 18:34:00",
            "version_normalized": "3.46.23",
            "business_identifiers": ["AG0019", "taskKey=358208"],
            "observations": ["涉及电梯切图和后续移动未继续下发"],
            "image_findings": [],
        },
        "paths": {
            "summary_snapshot_json": str(tmp_path / "summary_snapshot.json"),
        },
    }

    updated = pipeline_mod._ensure_riot_log_triage_state_ready(
        analysis_dir=analysis_dir,
        session={"title": "AG0019 乘梯后卡住", "analysis_mode": 1, "analysis_workspace": str(analysis_dir)},
        msg={"content": "继续分析 AG0019 乘梯卡住"},
        project_name="allspark",
        ones_artifacts=ones_result,
        ones_check={"status": "complete"},
    )

    assert updated["phase"] == "keywords_ready"
    assert updated["current_question"] == "围绕用户当前问题建立首轮证据锚点"
    assert updated["version_info"]["value"] == "3.46.23"
    assert updated["time_alignment"]["problem_timezone"] == "UTC+8"
    assert updated["time_alignment"]["normalized_problem_time"] == "2026-04-21 10:34:00"
    assert updated["time_alignment"]["normalized_window"]["start"] == "2026-04-21 10:04:00"
    assert updated["evidence_anchor"]["vehicle_name"] == "AG0019"
    assert updated["evidence_anchor"]["order_id"] == "358208"
    assert updated["keyword_package_status"] == "ready"
    assert updated["search_artifacts"]["dsl_round1"] == str(analysis_dir / "query.round1.dsl.txt")

    keyword_path = analysis_dir / "keyword_package.round1.json"
    dsl_path = analysis_dir / "query.round1.dsl.txt"
    assert keyword_path.exists()
    assert dsl_path.exists()
    keyword_package = json.loads(keyword_path.read_text(encoding="utf-8"))
    assert keyword_package["anchor_terms"] == ["AG0019", "358208"]
    assert keyword_package["dsl_query"] == dsl_path.read_text(encoding="utf-8").strip()
    assert '"AG0019"' in keyword_package["dsl_query"]
    assert '"358208"' in keyword_package["dsl_query"]
    assert keyword_package["gate_terms"] == []
    assert keyword_package["generic_terms"] == []
    assert keyword_package["target_files"] == []
    assert keyword_package["preferred_files"] == []
    assert keyword_package["excluded_files"] == []


@pytest.mark.asyncio
async def test_run_project_agent_filters_ones_and_passes_preferred_skill(tmp_path, monkeypatch):
    import core.pipeline as pipeline_mod
    import core.projects as projects_mod
    from core.orchestrator import agent_client as agent_client_mod

    captured: dict[str, object] = {}

    async def fake_run_for_project(**kwargs):
        captured.update(kwargs)
        return {"text": "ok", "session_id": "triage-session-002"}

    monkeypatch.setattr(
        projects_mod,
        "get_project",
        lambda name: SimpleNamespace(name=name, path=tmp_path, description="demo project"),
    )
    monkeypatch.setattr(
        projects_mod,
        "prepare_project_runtime_context",
        lambda project_name: SimpleNamespace(execution_path=tmp_path),
    )
    monkeypatch.setattr(projects_mod, "build_project_runtime_prompt_block", lambda runtime_context: "")
    monkeypatch.setattr(
        projects_mod,
        "merge_skills",
        lambda *args, **kwargs: {
            "ones": SimpleNamespace(description="intake", prompt="ones prompt"),
            "riot-log-triage": SimpleNamespace(description="triage", prompt="triage prompt"),
            "other": SimpleNamespace(description="other", prompt="other prompt"),
        },
    )
    monkeypatch.setattr(agent_client_mod.agent_client, "run_for_project", fake_run_for_project)

    result = await pipeline_mod._run_project_agent(
        "allspark",
        "排查 AG0019",
        "triage-session-001",
        runtime="codex",
        runtime_context=SimpleNamespace(execution_path=tmp_path),
        skill="riot-log-triage",
        exclude_skills={"ones"},
    )

    assert result["session_id"] == "triage-session-002"
    assert captured["skill"] == "riot-log-triage"
    assert set(captured["project_agents"]) == {"riot-log-triage", "other"}


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


def test_ones_cli_script_path_prefers_repo_skill(tmp_path, monkeypatch):
    import core.pipeline as pipeline_mod

    local_script = tmp_path / ".claude" / "skills" / "ones" / "scripts" / "ones_cli.py"
    local_script.parent.mkdir(parents=True, exist_ok=True)
    local_script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")

    monkeypatch.setattr(pipeline_mod, "settings", SimpleNamespace(project_root=tmp_path))

    assert pipeline_mod._ones_cli_script_path() == local_script


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


def test_collect_ones_text_fragments_prefers_summary_snapshot():
    import core.pipeline as pipeline_mod

    ones_result = {
        "task": {
            "summary": "原始标题",
            "description": "原始描述",
        },
        "summary_snapshot": {
            "status": "ready",
            "summary_text": "结构化摘要：问题发生在站点3，偶发触发失败。",
            "problem_time": "2026年4月10号上午11:30左右",
            "version_hint": "RIOT2.0",
            "version_fields": ["RIOT2.0-field"],
            "version_from_images": ["RIOT2.0-image"],
            "version_normalized": "RIOT2.0",
            "version_evidence": ["fields", "images"],
            "business_identifiers": ["站点3"],
            "observations": ["修改站点1/2编号后可触发"],
            "image_findings": ["版本截图显示 RIOT2.0"],
        },
    }

    fragments = pipeline_mod._collect_ones_text_fragments(ones_result, msg={"content": "帮我分析这个 ONES"})

    merged = "\n".join(fragments)
    assert "结构化摘要：问题发生在站点3" in merged
    assert "问题发生时间: 2026年4月10号上午11:30左右" in merged
    assert "版本线索: RIOT2.0" in merged
    assert "字段版本: RIOT2.0-field" in merged
    assert "图片版本: RIOT2.0-image" in merged
    assert "修改站点1/2编号后可触发" in merged
    assert "版本截图显示 RIOT2.0" in merged


def test_collect_ones_multimodal_image_paths_skips_after_snapshot_ready(tmp_path):
    import core.pipeline as pipeline_mod

    image_path = tmp_path / "desc.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nsnapshot")
    messages_json = tmp_path / "messages.json"
    messages_json.write_text(json.dumps({
        "description_images": [
            {"label": "desc-img", "path": str(image_path), "uuid": "img-1", "mime": "image/png"},
        ],
        "attachment_downloads": [],
    }, ensure_ascii=False), encoding="utf-8")

    ones_result = {
        "paths": {"messages_json": str(messages_json)},
        "summary_snapshot": {"status": "ready"},
    }

    assert pipeline_mod._collect_ones_multimodal_image_paths(ones_result) == []


def test_should_route_to_known_project_requires_project_without_agent_session():
    import core.pipeline as pipeline_mod

    assert pipeline_mod._should_route_to_known_project(
        {"project": "riot-standalone"},
        agent_session_id="",
        project_name="riot-standalone",
    ) is True
    assert pipeline_mod._should_route_to_known_project(
        {"project": "riot-standalone"},
        agent_session_id="sdk-session-1",
        project_name="riot-standalone",
    ) is False
    assert pipeline_mod._should_route_to_known_project(
        {"project": ""},
        agent_session_id="",
        project_name="",
    ) is False


@pytest.mark.asyncio
async def test_ensure_ones_summary_snapshot_writes_snapshot_and_updates_task_json(tmp_path, monkeypatch):
    import core.pipeline as pipeline_mod

    task_json = tmp_path / "task.json"
    task_json.write_text(json.dumps({
        "task": {"uuid": "task-demo-3", "summary": "demo"},
        "paths": {"task_json": str(task_json), "task_dir": str(tmp_path)},
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    ones_result = {
        "task": {"uuid": "task-demo-3", "summary": "demo"},
        "paths": {"task_json": str(task_json), "task_dir": str(tmp_path)},
    }

    async def fake_extract(*, msg, ones_result, runtime):
        return {
            "status": "ready",
            "summary_text": "结构化摘要",
            "problem_time": "2026-04-10 11:30",
            "problem_time_confidence": "high",
            "version_text": "RIOT2.0",
            "version_fields": ["RIOT2.0-field"],
            "version_from_images": ["RIOT2.0-image"],
            "version_normalized": "RIOT2.0",
            "version_evidence": ["text", "images"],
            "business_identifiers": ["站点3"],
            "observations": ["偶发，一个月一次"],
            "image_findings": ["截图中版本为 RIOT2.0"],
            "missing_items": [],
            "downloaded_files": [],
            "source": {"text_first": True, "images_consumed": True, "runtime": runtime},
        }

    monkeypatch.setattr(pipeline_mod, "_extract_ones_summary_snapshot", fake_extract)

    snapshot = await pipeline_mod._ensure_ones_summary_snapshot(
        msg={"content": "帮我分析这个 ONES"},
        ones_result=ones_result,
        runtime="codex",
    )

    snapshot_path = tmp_path / "summary_snapshot.json"
    assert snapshot is not None
    assert snapshot_path.exists()
    payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert payload["summary_text"] == "结构化摘要"
    assert payload["version_normalized"] == "RIOT2.0"
    assert payload["version_fields"] == ["RIOT2.0-field"]
    assert payload["version_from_images"] == ["RIOT2.0-image"]
    assert ones_result["summary_snapshot"]["summary_text"] == "结构化摘要"

    updated_task = json.loads(task_json.read_text(encoding="utf-8"))
    assert updated_task["summary_snapshot"]["summary_text"] == "结构化摘要"
    assert updated_task["paths"]["summary_snapshot_json"] == str(snapshot_path)


def test_merge_ones_summary_snapshot_collects_version_sources():
    import core.pipeline as pipeline_mod

    ones_result = {
        "task": {
            "description_local": "问题出现时间：2026年4月10号上午11:30左右\nRIOT2.0",
        },
        "named_fields": {
            "FMS/RIoT版本": "4.9.2",
        },
    }

    merged = pipeline_mod._merge_ones_summary_snapshot(
        llm_summary={
            "summary_text": "结构化摘要",
            "problem_time": "",
            "version_text": "",
            "version_fields": [],
            "version_from_images": ["截图里显示 4.9.2"],
            "version_normalized": "",
            "version_evidence": [],
            "business_identifiers": [],
            "observations": [],
            "image_findings": [],
            "missing_items": [],
        },
        ones_result=ones_result,
        msg={"content": "帮我分析这个 ONES"},
    )

    assert merged["version_fields"] == ["4.9.2"]
    assert merged["version_from_images"] == ["截图里显示 4.9.2"]
    assert merged["version_normalized"] == "2.0"
    assert "text" in merged["version_evidence"]
    assert "fields" in merged["version_evidence"]
    assert "images" in merged["version_evidence"]


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
        get_image_bytes=lambda image_key, **kwargs: (b"\x89PNGtriage-image", "capture.png"),
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


@pytest.mark.asyncio
async def test_stage_message_media_to_temp_resource_uses_project_temp_dir(tmp_path, monkeypatch):
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
                1, "feishu", "om_stage_001", "oc_x", "ou_x", "tester",
                "image", "[图片]", now, "",
                json.dumps({"type": "image", "image_key": "img_stage_001"}, ensure_ascii=False),
                "", "", "", "", None, 1, "pending", "", None, now,
            ),
        )
        await db.commit()

    monkeypatch.setattr(pipeline_mod, "DB_PATH", str(db_file))
    monkeypatch.setattr(pipeline_mod, "_resolve_temp_resource_root", lambda session: tmp_path / "allspark" / ".temp_resource")

    fake_client = SimpleNamespace(
        get_image_bytes=lambda image_key, *args, **kwargs: (b"\x89PNGstage-image", "stage.png"),
        get_file_bytes=lambda *args, **kwargs: None,
    )

    with patch("core.connectors.feishu.FeishuClient", return_value=fake_client):
        media_info = await pipeline_mod._stage_message_media_to_temp_resource(
            1,
            {
                "id": 1,
                "platform_message_id": "om_stage_001",
                "message_type": "image",
                "content": "[图片]",
                "media_info_json": json.dumps({"type": "image", "image_key": "img_stage_001"}, ensure_ascii=False),
                "attachment_path": "",
            },
            {"project": "allspark"},
        )

    assert media_info["download_status"] == "downloaded"
    assert "temp_resource_dir" in media_info
    staged_path = Path(media_info["staged_local_path"])
    assert staged_path.exists()
    assert staged_path.read_bytes() == b"\x89PNGstage-image"
    assert ".temp_resource" in str(staged_path)
    assert f"feishu\\{datetime.now().strftime('%Y%m%d')}\\om_stage_001\\original\\stage.png" in str(staged_path)

    refreshed = await pipeline_mod._read_message(1)
    assert refreshed is not None
    assert refreshed["attachment_path"] == str(staged_path.resolve())


@pytest.mark.asyncio
async def test_materialize_analysis_workspace_copies_from_temp_resource_cache(tmp_path, monkeypatch):
    import core.pipeline as pipeline_mod

    db_file = tmp_path / "app.sqlite"
    staged_path = tmp_path / "allspark" / ".temp_resource" / "feishu" / "20260423" / "om_img_cache_001" / "original" / "capture.png"
    staged_path.parent.mkdir(parents=True, exist_ok=True)
    staged_path.write_bytes(b"\x89PNGcached-image")

    async with aiosqlite.connect(db_file) as db:
        await db.executescript(_SCHEMA)
        now = datetime.now().isoformat()
        await db.execute(
            "INSERT INTO messages (id, platform, platform_message_id, chat_id, sender_id, sender_name, "
            "message_type, content, received_at, raw_payload, media_info_json, attachment_path, thread_id, root_id, parent_id, "
            "classified_type, session_id, pipeline_status, pipeline_error, processed_at, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                1, "feishu", "om_img_cache_001", "oc_x", "ou_x", "tester",
                "image", "[图片]", now, "",
                json.dumps({
                    "type": "image",
                    "image_key": "img_key_001",
                    "download_status": "downloaded",
                    "temp_resource_dir": str(staged_path.parent.parent.resolve()),
                    "staged_local_path": str(staged_path.resolve()),
                    "local_path": str(staged_path.resolve()),
                    "mime_type": "image/png",
                }, ensure_ascii=False),
                str(staged_path.resolve()), "", "", "", None, 1, "pending", "", None, now,
            ),
        )
        await db.execute(
            "INSERT INTO messages (id, platform, platform_message_id, chat_id, sender_id, sender_name, "
            "message_type, content, received_at, raw_payload, media_info_json, attachment_path, thread_id, root_id, parent_id, "
            "classified_type, session_id, pipeline_status, pipeline_error, processed_at, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                2, "feishu", "om_txt_cache_002", "oc_x", "ou_x", "tester",
                "text", "帮我分析刚才的截图", now, "",
                json.dumps({}, ensure_ascii=False),
                "", "", "", "", None, 1, "pending", "", None, now,
            ),
        )
        await db.commit()

    monkeypatch.setattr(pipeline_mod, "DB_PATH", str(db_file))
    monkeypatch.setattr(pipeline_mod, "_triage_base_dir", lambda: tmp_path / ".triage")

    fake_client = SimpleNamespace(
        get_image_bytes=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should reuse staged file")),
        get_file_bytes=lambda *args, **kwargs: None,
    )

    with patch("core.connectors.feishu.FeishuClient", return_value=fake_client):
        triage_dir = await pipeline_mod._materialize_analysis_workspace(
            2,
            {"id": 2, "content": "帮我分析刚才的截图", "platform_message_id": "om_txt_cache_002"},
            {"id": 1, "title": "截图问题分析", "topic": "", "project": "allspark"},
            1,
        )

    copied_path = triage_dir / "01-intake" / "attachments" / "om_img_cache_001" / "original" / "capture.png"
    assert copied_path.exists()
    assert copied_path.read_bytes() == b"\x89PNGcached-image"

    refreshed = await pipeline_mod._read_message(1)
    assert refreshed is not None
    assert refreshed["attachment_path"] == str(staged_path.resolve())
