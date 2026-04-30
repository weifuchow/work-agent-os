from datetime import datetime

import aiosqlite
import pytest

import core.projects as projects_mod
from core.pipeline import (
    _build_riot_log_seed_keyword_package,
    _contains_internal_tool_error,
    _match_explicit_project_switch,
)


def test_match_explicit_project_switch_prefers_riot3_frontend_alias(monkeypatch, tmp_path):
    monkeypatch.setattr(
        projects_mod,
        "get_projects",
        lambda: [
            projects_mod.ProjectConfig(
                name="allspark",
                path=tmp_path / "allspark",
                description=(
                    "Riot 调度系统 3.0（allspark）后端/调度核心。"
                    "别名：riot3、RIOT3、riot3调度系统。"
                ),
            ),
            projects_mod.ProjectConfig(
                name="riot-frontend-v3",
                path=tmp_path / "riot-frontend-v3",
                description=(
                    "RIOT3 前端项目（riot-frontend-v3）。"
                    "别名：riot3前端、riot3 前端、RIOT3前端、RIOT3 前端、"
                    "riot3 frontend、riot3-frontend、allspark frontend。"
                    "关键词线索：前端、frontend、Web UI、页面、控制台、Vue 3、Vite、Pinia。"
                ),
            ),
        ],
    )

    matched = _match_explicit_project_switch("riot3前端项目")
    assert matched is not None
    assert matched[0] == "riot-frontend-v3"

    matched = _match_explicit_project_switch("后续按 riot3 前端项目")
    assert matched is not None
    assert matched[0] == "riot-frontend-v3"


def test_match_explicit_project_switch_defaults_bare_riot3_to_allspark(monkeypatch, tmp_path):
    monkeypatch.setattr(
        projects_mod,
        "get_projects",
        lambda: [
            projects_mod.ProjectConfig(
                name="allspark",
                path=tmp_path / "allspark",
                description=(
                    "Riot 调度系统 3.0（allspark）后端/调度核心。"
                    "别名：riot3、RIOT3、riot3调度系统。"
                ),
            ),
            projects_mod.ProjectConfig(
                name="riot-frontend-v3",
                path=tmp_path / "riot-frontend-v3",
                description=(
                    "RIOT3 前端项目（riot-frontend-v3）。"
                    "别名：riot3前端、riot3 前端、RIOT3前端、RIOT3 前端、"
                    "riot3 frontend、riot3-frontend、allspark frontend。"
                ),
            ),
        ],
    )

    matched = _match_explicit_project_switch("riot3项目")
    assert matched is not None
    assert matched[0] == "allspark"

    matched = _match_explicit_project_switch("后续按 riot3 项目")
    assert matched is not None
    assert matched[0] == "allspark"


def test_riot_log_seed_package_routes_elevator_cross_map_order_execution():
    package = _build_riot_log_seed_keyword_package(
        project_name="allspark",
        issue_type="order_execution",
        vehicle_name="AG0019",
        order_id="",
        text=(
            "RIot3.46.23 小车 AG0019 在 2026-04-21 18:34 乘梯从 4 楼去往 5 楼，"
            "切换地图后一直在电梯里面不出来，十多分钟后才开始移动。"
        ),
        normalized_window={
            "start": "2026-04-21 10:04:00",
            "end": "2026-04-21 11:04:00",
        },
    )

    assert package["anchor_terms"] == ["AG0019"]
    assert "ChangeMapRequest" in package["gate_terms"]
    assert "MoveRequest" in package["gate_terms"]
    assert "CrossMapManager" in package["core_terms"]
    assert "ElevatorResourceDevice" in package["core_terms"]
    assert "vehicleProcState" in package["stage_terms"]
    assert "IN_CHANGE_MAP" in package["stage_terms"]
    assert "bootstrap" in package["target_files"]
    assert "reservation" in package["target_files"]
    assert "notify" in package["target_files"]
    assert package["excluded_files"] == ["metric"]
    assert package["preferred_files"][0] == "bootstrap"
    assert package["anchor_match_mode"] == "prefer"
    assert package["require_anchor"] is False
    assert package["require_gate_when_present"] is False
    assert '"AG0019"' in package["dsl_query"]
    assert '"ChangeMapRequest"' in package["dsl_query"]
    assert " OR " in package["dsl_query"]
    assert any(item["term"] == "CrossMapManager" for item in package["term_priorities"])


def test_internal_tool_error_detection_scans_rich_card_sections():
    assert _contains_internal_tool_error({
        "format": "rich",
        "title": "方格-10008 订单行为分析未完成",
        "summary": "已识别为 allspark/RIOT3 调度问题。",
        "sections": [
            {
                "title": "当前状态",
                "content": "dispatch_to_project 工具调用返回：user cancelled MCP tool call。",
            }
        ],
        "fallback_text": "已识别为 allspark/RIOT3 调度问题，但项目 Agent 调用被取消。",
    })


@pytest.mark.asyncio
async def test_route_session_creates_new_session_for_same_ones_task_without_thread(tmp_path, monkeypatch):
    import core.pipeline as pipeline_mod

    db_path = str(tmp_path / "app.sqlite")
    now = datetime.now().isoformat()
    old_link = "https://ones.standard-robots.com:10120/project/#/team/UNrQ5Ny5/task/NbJXtiyGP7R4vYnF"
    new_link = (
        "https://ones.standard-robots.com:10120/project/#/team/UNrQ5Ny5/"
        "project/UEjcYfJhPshGIUnd/component/OORdUJMu/view/H5TRzRtB/task/NbJXtiyGP7R4vYnF"
    )

    async with aiosqlite.connect(db_path) as db:
        await db.executescript("""
CREATE TABLE sessions (
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
    thread_id TEXT DEFAULT '',
    agent_runtime TEXT DEFAULT 'codex',
    summary_path TEXT DEFAULT '',
    last_active_at TEXT,
    message_count INTEGER DEFAULT 0,
    risk_level TEXT DEFAULT 'low',
    needs_manual_review INTEGER DEFAULT 0,
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform_message_id TEXT UNIQUE,
    chat_id TEXT,
    content TEXT,
    session_id INTEGER
);
""")
        cursor = await db.execute(
            "INSERT INTO sessions (session_key, source_chat_id, owner_user_id, title, status, last_active_at, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("session_ones_001", "oc_ones", "ou_ones", "#150552", "open", now, now, now),
        )
        session_id = cursor.lastrowid
        await db.execute(
            "INSERT INTO messages (platform_message_id, chat_id, content, session_id) VALUES (?,?,?,?)",
            ("om_old_ones", "oc_ones", old_link, session_id),
        )
        await db.commit()

    monkeypatch.setattr(pipeline_mod, "DB_PATH", db_path)

    routed = await pipeline_mod._route_session({
        "platform": "feishu",
        "platform_message_id": "om_new_ones",
        "chat_id": "oc_ones",
        "sender_id": "ou_ones",
        "content": new_link,
        "thread_id": "",
    })

    assert routed != session_id

    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute("SELECT thread_id, source_chat_id FROM sessions WHERE id = ?", (routed,))
        row = await cursor.fetchone()

    assert row == ("", "oc_ones")
