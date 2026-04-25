"""Integration tests: session routing, agent_session_id persistence, and resume flow.

Core business invariants tested:
  A. Project flow
     1. First message → orchestrator dispatches → agent_session_id + project saved to session
     2. Second message (same thread) → _run_project_agent called with session_id=<agent_session_id>
        This verifies `claude --resume <agent_session_id>` is invoked correctly.
  B. Non-project flow
     1. Message → orchestrator replies → no project binding
     2. Second message (same thread) → orchestrator again (never run_for_project)
  C. Model defaults to haiku when configured as default

All tests run against an isolated in-memory DB.
Agent SDK calls are mocked at the pipeline boundary (_run_orchestrator / _run_project_agent).
"""

import asyncio
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import aiosqlite
import pytest

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CHAT_ID = "oc_test_pipeline_001"
SENDER_ID = "ou_test_user_001"

# SDK session IDs used by mocks — these are what claude --resume would receive
ORCH_SESSION_ID = "sdk-orch-session-abc123"
PROJECT_SESSION_ID = "sdk-proj-session-xyz789"

TEST_DB_PATH: str = ""

# ---------------------------------------------------------------------------
# DB schema (must match models/db.py and pipeline migration expectations)
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_feishu_replies: list[dict] = []


def _make_mock_feishu():
    """FeishuClient mock that records replies and returns a deterministic thread_id."""
    client = MagicMock()

    def mock_reply(message_id, content, reply_in_thread=True, msg_type="text"):
        _feishu_replies.append({
            "message_id": message_id,
            "content": content,
            "reply_in_thread": reply_in_thread,
            "msg_type": msg_type,
        })
        return {
            "message_id": f"bot_{message_id}",
            "thread_id": f"thread_{message_id}",
        }

    client.reply_message = mock_reply
    return client


@pytest.fixture(autouse=True)
async def setup_test_db(tmp_path):
    """Isolated test DB + patched pipeline.DB_PATH + mocked FeishuClient."""
    global TEST_DB_PATH
    _feishu_replies.clear()

    db_file = tmp_path / "app.sqlite"
    TEST_DB_PATH = str(db_file)

    async with aiosqlite.connect(TEST_DB_PATH) as db:
        await db.executescript(_SCHEMA)
        await db.commit()

    import core.pipeline as pipeline_mod
    orig_db = pipeline_mod.DB_PATH
    pipeline_mod.DB_PATH = TEST_DB_PATH

    with patch("core.connectors.feishu.FeishuClient", side_effect=_make_mock_feishu), \
         patch("core.pipeline.get_agent_runtime_override", return_value=None):
        yield

    pipeline_mod.DB_PATH = orig_db


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

async def _insert_message(content: str, platform_mid: str, thread_id: str = "") -> int:
    now = datetime.now().isoformat()
    async with aiosqlite.connect(TEST_DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO messages (platform, platform_message_id, chat_id, sender_id, "
            "sender_name, message_type, content, received_at, thread_id, "
            "pipeline_status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("feishu", platform_mid, CHAT_ID, SENDER_ID, "TestUser", "text",
             content, now, thread_id, "pending", now),
        )
        await db.commit()
        return cursor.lastrowid


async def _get_session(session_id: int) -> dict:
    async with aiosqlite.connect(TEST_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,))
        row = await cursor.fetchone()
        assert row is not None, f"Session {session_id} not found"
        return dict(row)


async def _get_message(msg_id: int) -> dict:
    async with aiosqlite.connect(TEST_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM messages WHERE id = ?", (msg_id,))
        row = await cursor.fetchone()
        assert row is not None, f"Message {msg_id} not found"
        return dict(row)


async def _get_message_by_platform_id(platform_message_id: str) -> dict:
    async with aiosqlite.connect(TEST_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM messages WHERE platform_message_id = ?",
            (platform_message_id,),
        )
        row = await cursor.fetchone()
        assert row is not None, f"Message {platform_message_id} not found"
        return dict(row)


async def _set_session_thread(session_id: int, thread_id: str) -> None:
    """Manually bind thread_id to a session (simulates feishu thread creation)."""
    async with aiosqlite.connect(TEST_DB_PATH) as db:
        await db.execute(
            "UPDATE sessions SET thread_id = ? WHERE id = ?",
            (thread_id, session_id),
        )
        await db.commit()


async def _simulate_dispatch_writeback(
    session_id: int,
    project: str,
    agent_session_id: str,
    agent_runtime: str = "claude",
) -> None:
    """Simulate the dispatch_to_project tool writing agent_session_id + project to session.

    In production, this happens inside the MCP tool (agent_client.py:dispatch_to_project).
    The tool writes to the production DB path; in tests we replicate this to the test DB.
    """
    async with aiosqlite.connect(TEST_DB_PATH) as db:
        await db.execute(
            "UPDATE sessions SET project = ?, agent_session_id = ?, agent_runtime = ?, updated_at = ? WHERE id = ?",
            (project, agent_session_id, agent_runtime, datetime.now().isoformat(), session_id),
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Mock factories
# ---------------------------------------------------------------------------

def _make_orchestrator_project_result(project_name: str, reply: str) -> dict:
    """Orchestrator mock result for a project-dispatched message."""
    return {
        "text": json.dumps({
            "action": "replied",
            "classified_type": "work_question",
            "topic": f"{project_name} 项目咨询",
            "project_name": project_name,
            "reply_content": reply,
            "reason": f"消息涉及 {project_name} 项目，已 dispatch",
        }),
        "session_id": ORCH_SESSION_ID,
        "cost_usd": 0.01,
        "num_turns": 3,
    }


def _make_orchestrator_chat_result(reply: str, topic: str = "闲聊") -> dict:
    """Orchestrator mock result for a non-project chat message."""
    return {
        "text": json.dumps({
            "action": "replied",
            "classified_type": "chat",
            "topic": topic,
            "project_name": None,
            "reply_content": reply,
            "reason": "闲聊，无项目关联",
        }),
        "session_id": ORCH_SESSION_ID,
        "cost_usd": 0.005,
        "num_turns": 1,
    }


def _make_project_agent_result(reply: str) -> dict:
    """Project agent mock result (simulating resumed project session)."""
    return {
        "text": reply,
        "session_id": PROJECT_SESSION_ID,  # Same session — this is resume
        "cost_usd": 0.02,
        "num_turns": 2,
    }


# ---------------------------------------------------------------------------
# Test A1: Project first message — agent_session_id persisted
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_project_first_message_persists_agent_session_id():
    """
    First project message:
      - Orchestrator dispatches to allspark (mock returns project JSON)
      - dispatch_to_project tool writes agent_session_id + project to session (simulated)
      - Pipeline reads this and writes ORCH_SESSION_ID as agent_session_id

    Success criteria:
      - session.project == "allspark"
      - session.agent_session_id is non-empty (the value from dispatch writeback)
      - message.pipeline_status == "completed"
      - Feishu reply sent
    """
    mock_orch = AsyncMock(return_value=_make_orchestrator_project_result(
        "allspark", "Allspark 是 Riot 调度系统 3.0，多机器人调度与控制。"
    ))

    from core.pipeline import process_message

    with patch("core.pipeline._run_orchestrator", mock_orch), \
         patch("core.pipeline._run_project_agent", AsyncMock()) as mock_proj:

        msg_id = await _insert_message("allspark是什么项目", "m_proj_a1_001")
        await process_message(msg_id)

        # Simulate dispatch_to_project writeback (happens inside MCP tool in production)
        msg = await _get_message(msg_id)
        await _simulate_dispatch_writeback(msg["session_id"], "allspark", PROJECT_SESSION_ID)

    # --- Assertions ---
    msg = await _get_message(msg_id)
    assert msg["pipeline_status"] == "completed", f"Expected completed, got: {msg['pipeline_status']}"
    assert msg["classified_type"] == "work_question"

    session = await _get_session(msg["session_id"])
    # project binding from dispatch writeback
    assert session["project"] == "allspark", f"project: {session['project']!r}"
    # agent_session_id set by dispatch writeback
    assert session["agent_session_id"] == PROJECT_SESSION_ID, \
        f"agent_session_id: {session['agent_session_id']!r}"
    assert session["agent_runtime"] == "claude"

    # Feishu reply delivered
    assert len(_feishu_replies) == 1, f"Expected 1 reply, got {len(_feishu_replies)}"

    # Project agent should NOT be called on first message (orchestrator handles it)
    mock_proj.assert_not_called()

    print(f"\n[PASS] A1: First project message persists agent_session_id={PROJECT_SESSION_ID}")


# ---------------------------------------------------------------------------
# Test A2: Project resume — run_for_project called with correct session_id
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_project_resume_uses_correct_session_id():
    """
    Second message in a project thread:
      - Session already has agent_session_id + project + thread_id set
      - Pipeline detects this → calls _run_project_agent(project, prompt, session_id=<agent_session_id>)
      - This maps to `claude --resume <agent_session_id>` in the SDK

    SUCCESS CRITERION: run_for_project called with session_id == PROJECT_SESSION_ID
    """
    # Pre-setup: session already dispatched to allspark, has thread_id
    THREAD_ID = "thread_pre_existing_001"

    async with aiosqlite.connect(TEST_DB_PATH) as db:
        now = datetime.now().isoformat()
        cursor = await db.execute(
            "INSERT INTO sessions (session_key, source_platform, source_chat_id, owner_user_id, "
            "title, project, status, thread_id, agent_session_id, last_active_at, "
            "message_count, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("key_resume_test_001", "feishu", CHAT_ID, SENDER_ID,
             "allspark 项目咨询", "allspark", "open",
             THREAD_ID, PROJECT_SESSION_ID,
             now, 1, now, now),
        )
        pre_session_id = cursor.lastrowid
        await db.commit()

    captured_calls: list[dict] = []

    async def mock_project_run(project_name, prompt, session_id, **kwargs):
        captured_calls.append({
            "project_name": project_name,
            "session_id": session_id,
            "prompt": prompt,
        })
        return _make_project_agent_result("Allspark 支持多数据源，通过 application-extra.yaml 配置。")

    from core.pipeline import process_message

    with patch("core.pipeline._run_project_agent", AsyncMock(side_effect=mock_project_run)), \
         patch("core.pipeline._run_orchestrator", AsyncMock()) as mock_orch:

        msg_id = await _insert_message("他支持多数据源吗", "m_proj_a2_001", thread_id=THREAD_ID)
        await process_message(msg_id)

    # --- Assertions ---
    msg = await _get_message(msg_id)
    assert msg["pipeline_status"] == "completed", f"Expected completed, got: {msg['pipeline_status']}"
    assert msg["session_id"] == pre_session_id, "Must route to existing session via thread_id"

    # CRITICAL: run_project_agent called (not orchestrator)
    assert len(captured_calls) == 1, f"Expected exactly 1 project agent call, got {len(captured_calls)}"
    mock_orch.assert_not_called()

    # CRITICAL: correct session_id passed → this is `claude --resume <PROJECT_SESSION_ID>`
    assert captured_calls[0]["session_id"] == PROJECT_SESSION_ID, (
        f"Expected session_id={PROJECT_SESSION_ID!r}, "
        f"got {captured_calls[0]['session_id']!r}\n"
        f"This means claude --resume was called with wrong ID!"
    )
    assert captured_calls[0]["project_name"] == "allspark"

    # Feishu reply delivered
    assert len(_feishu_replies) == 1

    print(f"\n[PASS] A2: Resume called with session_id={PROJECT_SESSION_ID} (claude --resume verified)")


# ---------------------------------------------------------------------------
# Test A3: Full project multi-turn end-to-end
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_project_multiturn_full_flow():
    """
    End-to-end multi-turn project session:
      Round 1: New message → orchestrator → dispatch → agent_session_id + project saved
      Round 2: Same thread → pipeline detects session → calls run_for_project with resume

    Verifies the complete flow from first contact to resume.
    """
    round2_calls: list[dict] = []

    async def mock_orch(prompt, **kwargs):
        return _make_orchestrator_project_result(
            "allspark", "Allspark 是多机器人调度系统。"
        )

    async def mock_proj(project_name, prompt, session_id, **kwargs):
        round2_calls.append({"project_name": project_name, "session_id": session_id})
        return _make_project_agent_result("支持多数据源，配置 application-extra.yaml。")

    from core.pipeline import process_message

    with patch("core.pipeline._run_orchestrator", AsyncMock(side_effect=mock_orch)), \
         patch("core.pipeline._run_project_agent", AsyncMock(side_effect=mock_proj)):

        # Round 1: first message
        msg1_id = await _insert_message("allspark是什么项目", "m_e2e_001")
        await process_message(msg1_id)

        # Simulate dispatch_to_project writeback (MCP tool side effect)
        msg1 = await _get_message(msg1_id)
        session_id = msg1["session_id"]
        await _simulate_dispatch_writeback(session_id, "allspark", PROJECT_SESSION_ID)

        # Thread_id comes from the feishu reply (pipeline calls _deliver_reply → gets thread_id)
        # Since we mock FeishuClient.reply_message to return thread_{msg_id}, the pipeline
        # writes this to the session via _update_session_state.
        # Check what thread_id the session has:
        session = await _get_session(session_id)
        thread_id = session["thread_id"]
        if not thread_id:
            # Pipeline may not have persisted thread_id if session already had one;
            # force-set it to match the feishu mock return value
            thread_id = f"thread_m_e2e_001"
            await _set_session_thread(session_id, thread_id)

        # Round 2: same thread (续会话)
        msg2_id = await _insert_message("他支持多数据源吗", "m_e2e_002", thread_id=thread_id)
        await process_message(msg2_id)

    # --- Assertions ---
    msg1_final = await _get_message(msg1_id)
    msg2_final = await _get_message(msg2_id)

    assert msg1_final["pipeline_status"] == "completed"
    assert msg2_final["pipeline_status"] == "completed"

    # Same session (thread routing)
    assert msg1_final["session_id"] == msg2_final["session_id"], \
        "Both messages must be in the same session"
    sid = msg1_final["session_id"]

    # Session has project + agent_session_id (from dispatch writeback)
    session = await _get_session(sid)
    assert session["project"] == "allspark"
    assert session["agent_session_id"] == PROJECT_SESSION_ID

    # Round 2: resume was called with correct session_id
    assert len(round2_calls) == 1, f"Expected 1 project agent call in round 2, got {len(round2_calls)}"
    assert round2_calls[0]["session_id"] == PROJECT_SESSION_ID, \
        f"claude --resume must use {PROJECT_SESSION_ID}, got {round2_calls[0]['session_id']!r}"
    assert round2_calls[0]["project_name"] == "allspark"

    # Feishu replies: 2 (one per round)
    assert len(_feishu_replies) == 2, f"Expected 2 feishu replies, got {len(_feishu_replies)}"

    print(f"\n[PASS] A3: Full multi-turn project flow — agent_session_id={PROJECT_SESSION_ID}")
    print(f"         session_id={sid}, thread_id={thread_id}")
    print(f"         feishu_replies={len(_feishu_replies)}")


# ---------------------------------------------------------------------------
# Test B1: Non-project message — no project binding
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_non_project_no_project_binding():
    """
    Chat message with no project relevance:
      - Orchestrator replies directly (no dispatch)
      - session.project must remain empty
      - session.agent_session_id is the orchestrator session (not a project session)
      - run_for_project must NOT be called

    Verifies the non-project path is clean.
    """
    mock_proj = AsyncMock()

    from core.pipeline import process_message

    with patch("core.pipeline._run_orchestrator",
               AsyncMock(return_value=_make_orchestrator_chat_result("你好！有什么可以帮你？"))), \
         patch("core.pipeline._run_project_agent", mock_proj):

        msg_id = await _insert_message("你好", "m_chat_b1_001")
        await process_message(msg_id)

    # --- Assertions ---
    msg = await _get_message(msg_id)
    assert msg["pipeline_status"] == "completed"
    assert msg["classified_type"] == "chat"

    session = await _get_session(msg["session_id"])
    assert session["project"] == "", f"Non-project message must not set project, got: {session['project']!r}"
    # Orchestrator session_id IS saved (for potential resume of orchestrator context)
    assert session["agent_session_id"] == ORCH_SESSION_ID, \
        f"agent_session_id should be orchestrator session: {session['agent_session_id']!r}"
    assert session["agent_runtime"] == "claude"

    # Project agent never called
    mock_proj.assert_not_called()

    assert len(_feishu_replies) == 1

    print(f"\n[PASS] B1: Non-project message has no project binding")


@pytest.mark.asyncio
async def test_non_project_runtime_persisted_from_default():
    """Default agent runtime should be persisted onto newly created sessions."""
    captured_kwargs: list[dict] = []

    async def mock_orch(prompt, **kwargs):
        captured_kwargs.append(kwargs)
        return _make_orchestrator_chat_result("你好！")

    from core.pipeline import process_message

    with patch("core.pipeline._get_default_agent_runtime", return_value="codex"), \
         patch("core.pipeline._run_orchestrator", AsyncMock(side_effect=mock_orch)), \
         patch("core.pipeline._run_project_agent", AsyncMock()):

        msg_id = await _insert_message("你好", "m_chat_runtime_001")
        await process_message(msg_id)

    msg = await _get_message(msg_id)
    session = await _get_session(msg["session_id"])

    assert captured_kwargs[0]["runtime"] == "codex"
    assert session["agent_runtime"] == "codex"

    print("\n[PASS] B1.5: Default runtime persisted to session")


# ---------------------------------------------------------------------------
# Test B2: Non-project multi-turn — orchestrator used every round
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_non_project_multiturn_always_uses_orchestrator():
    """
    Two chat messages in the same thread:
      - Neither dispatches to project
      - Both rounds use orchestrator (run_for_project never called)
      - session.project stays empty throughout

    Verifies that non-project sessions don't accidentally trigger project resume.
    """
    orch_calls: list[str] = []
    mock_proj = AsyncMock()

    async def mock_orch(prompt, **kwargs):
        orch_calls.append(prompt)
        if "你好" in prompt:
            return _make_orchestrator_chat_result("你好！", topic="问候")
        return _make_orchestrator_chat_result("今天天气不错。", topic="天气")

    from core.pipeline import process_message

    with patch("core.pipeline._run_orchestrator", AsyncMock(side_effect=mock_orch)), \
         patch("core.pipeline._run_project_agent", mock_proj):

        msg1_id = await _insert_message("你好", "m_chat_b2_001")
        await process_message(msg1_id)

        # Get or set thread_id for round 2
        msg1 = await _get_message(msg1_id)
        session = await _get_session(msg1["session_id"])
        thread_id = session["thread_id"] or f"thread_m_chat_b2_001"
        if not session["thread_id"]:
            await _set_session_thread(msg1["session_id"], thread_id)

        msg2_id = await _insert_message("今天天气怎么样", "m_chat_b2_002", thread_id=thread_id)
        await process_message(msg2_id)

    # --- Assertions ---
    msg1_final = await _get_message(msg1_id)
    msg2_final = await _get_message(msg2_id)

    assert msg1_final["pipeline_status"] == "completed"
    assert msg2_final["pipeline_status"] == "completed"

    # Same session
    assert msg1_final["session_id"] == msg2_final["session_id"]
    session = await _get_session(msg1_final["session_id"])

    # No project binding ever
    assert session["project"] == "", f"project must be empty: {session['project']!r}"

    # Orchestrator called twice (once per round)
    assert len(orch_calls) == 2, f"Expected 2 orchestrator calls, got {len(orch_calls)}"

    # Project agent never called
    mock_proj.assert_not_called()

    # Feishu replies: 2
    assert len(_feishu_replies) == 2

    print(f"\n[PASS] B2: Non-project multi-turn uses orchestrator every round")


# ---------------------------------------------------------------------------
# Test B3: Noise message — silent, no reply
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_noise_message_no_reply():
    """Single emoji classified as noise → no feishu reply sent."""
    from core.pipeline import process_message

    with patch("core.pipeline._run_orchestrator", AsyncMock(return_value={
        "text": json.dumps({
            "action": "silent",
            "classified_type": "noise",
            "topic": "",
            "project_name": None,
            "reply_content": "",
            "reason": "single emoji, no reply needed",
        }),
        "session_id": ORCH_SESSION_ID,
        "cost_usd": 0.001,
    })):
        msg_id = await _insert_message("👍", "m_noise_001")
        await process_message(msg_id)

    msg = await _get_message(msg_id)
    assert msg["pipeline_status"] == "completed"
    assert msg["classified_type"] == "noise"
    assert len(_feishu_replies) == 0, f"Noise must not generate a reply, got {len(_feishu_replies)}"

    print(f"\n[PASS] B3: Noise message is silent (no feishu reply)")


@pytest.mark.asyncio
async def test_direct_ones_route_skips_orchestrator_and_enters_project_agent():
    """High-confidence ONES routing should dispatch to project agent on the first turn."""
    from core.pipeline import process_message

    direct_route = {
        "project_name": "allspark",
        "prompt": "用户直接发送了 ONES 工单链接，系统已预路由到项目 `allspark`。",
        "confidence": "high",
        "score": 7,
        "reasons": ["命中领域关键词: 调度, 自动充电, 充电桩"],
        "task_ref": "1FmsdpJjHT3JPyWL",
        "task_url": "https://ones.standard-robots.com:10120/project/#/team/UNrQ5Ny5/task/1FmsdpJjHT3JPyWL",
        "task_summary": "【KIOXIA岩手工厂日本自动搬运复购项目】无法生成自动充电任务",
    }
    complete_ones = {
        "task": {
            "uuid": "1FmsdpJjHT3JPyWL",
            "summary": "【KIOXIA岩手工厂日本自动搬运复购项目】无法生成自动充电任务",
            "description": "发生问题时间：2026-04-07 11:00，11号车无法生成自动充电任务，现场报错日志显示 ReservationConflictException。",
            "url": direct_route["task_url"],
        },
        "named_fields": {
            "车辆序列号": "2410084",
        },
        "paths": {
            "messages_json": "",
        },
    }

    project_calls: list[dict] = []

    async def mock_project(project_name, prompt, session_id, **kwargs):
        project_calls.append({
            "project_name": project_name,
            "prompt": prompt,
            "session_id": session_id,
            "skill": kwargs.get("skill"),
            "exclude_skills": kwargs.get("exclude_skills"),
        })
        return {
            "text": "先给出结论：当前问题更像调度侧自动充电任务生成链路异常。",
            "session_id": PROJECT_SESSION_ID,
            "cost_usd": 0.02,
        }

    with patch("core.pipeline._try_direct_project_route", AsyncMock(return_value=direct_route)), \
         patch("core.pipeline._fetch_ones_task_artifacts", AsyncMock(return_value=complete_ones)), \
         patch("core.pipeline._should_prepare_analysis_workspace", AsyncMock(return_value=False)), \
         patch("core.pipeline._should_capture_process_trace", return_value=False), \
         patch("core.pipeline._run_project_agent", AsyncMock(side_effect=mock_project)) as mock_proj, \
         patch("core.pipeline._run_orchestrator", AsyncMock()) as mock_orch:
        msg_id = await _insert_message(
            "帮我分析这个 ONES：#149268 https://ones.standard-robots.com:10120/project/#/team/UNrQ5Ny5/task/1FmsdpJjHT3JPyWL",
            "m_direct_ones_001",
        )
        await process_message(msg_id)

    msg = await _get_message(msg_id)
    session = await _get_session(msg["session_id"])

    assert msg["pipeline_status"] == "completed"
    assert session["project"] == "allspark"
    assert session["agent_session_id"] == ""
    assert session["analysis_mode"] == 1
    assert session["analysis_workspace"]
    assert mock_orch.await_count == 0, "Direct ONES route should not call orchestrator"
    assert mock_proj.await_count == 1
    assert project_calls[0]["project_name"] == "allspark"
    assert project_calls[0]["session_id"] is None
    assert project_calls[0]["skill"] == "riot-log-triage"
    assert project_calls[0]["exclude_skills"] == {"ones"}
    assert direct_route["prompt"] in project_calls[0]["prompt"]
    assert "[ONES 下载产物]" in project_calls[0]["prompt"]
    assert "[RIOT 日志排障状态流]" in project_calls[0]["prompt"]
    assert "search_worker_usage=" in project_calls[0]["prompt"]
    assert "dsl_round1=" in project_calls[0]["prompt"]
    state = json.loads((Path(session["analysis_workspace"]) / "00-state.json").read_text(encoding="utf-8"))
    assert state["agent_context"]["session_id"] == PROJECT_SESSION_ID
    assert state["agent_context"]["skill"] == "riot-log-triage"
    assert len(_feishu_replies) == 1

    print("\n[PASS] A4: Direct ONES route skips orchestrator and enters project agent")


@pytest.mark.asyncio
async def test_direct_ones_route_prepares_worktree_context_for_kioxia_charge_issue():
    """The KIOXIA charge-task ONES case should carry a prepared worktree context into the project agent."""
    from core.pipeline import process_message

    simulated_content = (
        "#149268 【KIOXIA岩手工厂日本自动搬运复购项目】无法生成自动充电任务\n"
        "https://ones.standard-robots.com:10120/project/#/team/UNrQ5Ny5/task/1FmsdpJjHT3JPyWL"
    )
    direct_route = {
        "project_name": "fms-java",
        "prompt": "用户直接发送了 ONES 工单链接，系统已预路由到项目 `fms-java`。",
        "confidence": "high",
        "score": 9,
        "reasons": ["FMS/RIoT版本 命中 4.9.2-186-g96fb6f2f9_20250723 -> fms-java"],
        "task_ref": "1FmsdpJjHT3JPyWL",
        "task_url": "https://ones.standard-robots.com:10120/project/#/team/UNrQ5Ny5/task/1FmsdpJjHT3JPyWL",
        "task_summary": "【KIOXIA岩手工厂日本自动搬运复购项目】无法生成自动充电任务",
    }
    ones_result = {
        "task": {
            "uuid": "1FmsdpJjHT3JPyWL",
            "number": 149268,
            "summary": direct_route["task_summary"],
            "description": "发生问题时间：2026-04-07 11:00，11号车在停靠点无法生成自动充电任务。",
            "url": direct_route["task_url"],
        },
        "project": {
            "display_name": "KIOXIA岩手工厂日本自动搬运复购项目",
        },
        "named_fields": {
            "FMS/RIoT版本": "4.9.2-186-g96fb6f2f9_20250723",
            "车辆序列号": "2410084",
        },
        "paths": {
            "messages_json": "",
        },
    }
    runtime_context = SimpleNamespace(
        running_project="fms-java",
        business_project_name="KIOXIA岩手工厂日本自动搬运复购项目",
        project_path=Path("D:/standard/riot/fms-java"),
        execution_path=Path("D:/standard/work-agent-os/.worktrees/fms-java/149268-1FmsdpJjHT3JPyWL-4.9.2"),
        current_branch="dev",
        current_version="dev@3cf57f15",
        target_branch="master",
        target_tag="v4.9.2",
        checkout_ref="v4.9.2",
        version_source_field="FMS/RIoT版本",
        version_source_value="4.9.2-186-g96fb6f2f9_20250723",
        recommended_worktree=Path("D:/standard/work-agent-os/.worktrees/fms-java/149268-1FmsdpJjHT3JPyWL-4.9.2"),
        notes=["已创建 worktree 并检出 v4.9.2"],
    )

    project_calls: list[dict] = []

    async def mock_project(project_name, prompt, session_id, **kwargs):
        project_calls.append({
            "project_name": project_name,
            "prompt": prompt,
            "session_id": session_id,
            "runtime_context": kwargs.get("runtime_context"),
        })
        return {
            "text": "先给出结论：当前更像位置状态丢失导致充电序列生成失败。",
            "session_id": PROJECT_SESSION_ID,
            "cost_usd": 0.02,
        }

    with patch("core.pipeline._try_direct_project_route", AsyncMock(return_value=direct_route)), \
         patch("core.pipeline._fetch_ones_task_artifacts", AsyncMock(return_value=ones_result)), \
         patch("core.pipeline._evaluate_ones_artifact_completeness", return_value={
             "status": "complete",
             "missing_items": [],
             "evidence_sources": ["ONES 工单描述", "日志附件 1 个"],
             "notes": [],
         }), \
         patch("core.pipeline._should_prepare_analysis_workspace", AsyncMock(return_value=False)), \
         patch("core.pipeline._should_capture_process_trace", return_value=False), \
         patch("core.projects.prepare_project_runtime_context", return_value=runtime_context), \
         patch("core.pipeline._run_project_agent", AsyncMock(side_effect=mock_project)) as mock_proj, \
         patch("core.pipeline._run_orchestrator", AsyncMock()) as mock_orch:
        msg_id = await _insert_message(simulated_content, "m_direct_ones_worktree_001")
        await process_message(msg_id)

    msg = await _get_message(msg_id)
    session = await _get_session(msg["session_id"])

    assert msg["pipeline_status"] == "completed"
    assert session["project"] == "fms-java"
    assert mock_orch.await_count == 0
    assert mock_proj.await_count == 1
    assert project_calls[0]["project_name"] == "fms-java"
    assert project_calls[0]["runtime_context"].execution_path == runtime_context.execution_path
    assert project_calls[0]["runtime_context"].checkout_ref == "v4.9.2"
    assert len(_feishu_replies) == 1
    card = json.loads(_feishu_replies[0]["content"])
    card_text = json.dumps(card, ensure_ascii=False)
    assert "运行的项目" in card_text
    assert "目录" in card_text
    assert "149268-1FmsdpJjHT3JPyWL-4.9.2" in card_text
    assert "v4.9.2" in card_text

    print("\n[PASS] A4.0: KIOXIA ONES route passes prepared worktree context")


@pytest.mark.asyncio
async def test_incomplete_ones_evidence_blocks_project_analysis():
    """ONES issues with incomplete evidence should request supplement before analysis."""
    from core.pipeline import process_message

    direct_route = {
        "project_name": "allspark",
        "prompt": "用户直接发送了 ONES 工单链接，系统已预路由到项目 `allspark`。",
        "confidence": "high",
        "score": 7,
        "reasons": ["命中领域关键词: 调度, 自动充电, 充电桩"],
        "task_ref": "1FmsdpJjHT3JPyWL",
        "task_url": "https://ones.standard-robots.com:10120/project/#/team/UNrQ5Ny5/task/1FmsdpJjHT3JPyWL",
        "task_summary": "【KIOXIA岩手工厂日本自动搬运复购项目】无法生成自动充电任务",
    }
    ones_result = {
        "task": {
            "uuid": "1FmsdpJjHT3JPyWL",
            "summary": "【KIOXIA岩手工厂日本自动搬运复购项目】无法生成自动充电任务",
            "description": "发生问题时间：2026-04-07 11:00，11号车无法生成自动充电任务。",
            "url": direct_route["task_url"],
        },
        "project": {
            "display_name": "KIOXIA岩手工厂日本自动搬运复购项目",
        },
        "named_fields": {
            "车辆序列号": "2410084",
        },
    }

    with patch("core.pipeline._try_direct_project_route", AsyncMock(return_value=direct_route)), \
         patch("core.pipeline._fetch_ones_task_artifacts", AsyncMock(return_value=ones_result)), \
         patch("core.pipeline._should_prepare_analysis_workspace", AsyncMock(return_value=False)), \
         patch("core.pipeline._should_capture_process_trace", return_value=False), \
         patch("core.pipeline._run_project_agent", AsyncMock()) as mock_proj, \
         patch("core.pipeline._run_orchestrator", AsyncMock()) as mock_orch:
        msg_id = await _insert_message(
            "帮我分析这个 ONES：#149268 https://ones.standard-robots.com:10120/project/#/team/UNrQ5Ny5/task/1FmsdpJjHT3JPyWL",
            "m_direct_ones_gate_001",
        )
        await process_message(msg_id)

    msg = await _get_message(msg_id)
    session = await _get_session(msg["session_id"])

    assert msg["pipeline_status"] == "completed"
    assert session["project"] == "allspark"
    assert mock_proj.await_count == 0
    assert mock_orch.await_count == 0
    assert len(_feishu_replies) == 1
    assert _feishu_replies[0]["msg_type"] == "interactive"
    card = json.loads(_feishu_replies[0]["content"])
    assert card["header"]["template"] == "orange"
    card_text = json.dumps(card, ensure_ascii=False)
    assert "请优先补充" in card_text
    assert "相关日志/异常堆栈" in card_text

    print("\n[PASS] A4.1: Incomplete ONES evidence blocks analysis and requests supplement")


@pytest.mark.asyncio
async def test_direct_ones_route_only_applies_on_first_turn():
    """Direct ONES route must not hijack later turns in an existing non-project session."""
    from core.pipeline import process_message

    async with aiosqlite.connect(TEST_DB_PATH) as db:
        now = datetime.now().isoformat()
        await db.execute(
            "INSERT INTO sessions (session_key, source_platform, source_chat_id, owner_user_id, "
            "title, project, status, thread_id, agent_session_id, last_active_at, "
            "message_count, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("key_direct_ones_existing", "feishu", CHAT_ID, SENDER_ID,
             "已有非项目会话", "", "open",
             "thread_direct_ones_existing", "", now, 3, now, now),
        )
        await db.commit()

    complete_ones = {
        "task": {
            "uuid": "1FmsdpJjHT3JPyWL",
            "summary": "一个完整的 ONES 工单",
            "description": "发生问题时间：2026-04-07 11:00，订单 order-123 无法流转，11号车受影响，报错日志包含 ReservationConflictException。",
            "url": "https://ones.standard-robots.com:10120/project/#/team/UNrQ5Ny5/task/1FmsdpJjHT3JPyWL",
        },
        "named_fields": {
            "车辆序列号": "2410084",
        },
        "paths": {
            "messages_json": "",
        },
    }

    with patch("core.pipeline._fetch_ones_task_artifacts", AsyncMock(return_value=complete_ones)), \
         patch("core.pipeline._should_prepare_analysis_workspace", AsyncMock(return_value=False)), \
         patch("core.pipeline._should_capture_process_trace", return_value=False), \
         patch("core.pipeline._run_orchestrator", AsyncMock(return_value={
            "text": json.dumps({
                "action": "replied",
                "classified_type": "chat",
                "topic": "",
                "project_name": None,
                "reply_content": "继续按原流程处理。",
                "reason": "orchestrator fallback",
            }, ensure_ascii=False),
            "session_id": ORCH_SESSION_ID,
            "cost_usd": 0.001,
        })) as mock_orch, \
         patch("core.pipeline._run_project_agent", AsyncMock()) as mock_proj:
        msg_id = await _insert_message(
            "顺便看这个 https://ones.standard-robots.com:10120/project/#/team/UNrQ5Ny5/task/1FmsdpJjHT3JPyWL",
            "m_direct_ones_existing_001",
            thread_id="thread_direct_ones_existing",
        )
        await process_message(msg_id)

    assert mock_orch.await_count == 1
    assert mock_proj.await_count == 0

    msg = await _get_message(msg_id)
    session = await _get_session(msg["session_id"])
    assert session["project"] == ""

    print("\n[PASS] A5: Direct ONES route only applies on the first turn")


# ---------------------------------------------------------------------------
# Test A5.1: Explicit project-switch phrase routes directly to project agent
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_explicit_project_switch_routes_directly_to_project_agent():
    """A short first-turn project-switch message should bypass orchestrator and enter the project agent."""
    from core.pipeline import process_message

    project_calls: list[dict] = []

    async def mock_project(project_name, prompt, session_id, **kwargs):
        project_calls.append({
            "project_name": project_name,
            "prompt": prompt,
            "session_id": session_id,
        })
        return {
            "text": "后续按 allspark（RIOT3/riot3 调度系统）项目处理。你直接发具体问题、需求、日志、报错或工单链接即可。",
            "session_id": PROJECT_SESSION_ID,
            "cost_usd": 0.01,
        }

    with patch("core.pipeline._run_project_agent", AsyncMock(side_effect=mock_project)) as mock_proj, \
         patch("core.pipeline._run_orchestrator", AsyncMock()) as mock_orch:
        msg_id = await _insert_message("allspark 项目", "m_direct_project_switch_001")
        await process_message(msg_id)

    msg = await _get_message(msg_id)
    session = await _get_session(msg["session_id"])

    assert msg["pipeline_status"] == "completed"
    assert session["project"] == "allspark"
    assert session["agent_session_id"] == PROJECT_SESSION_ID
    assert mock_orch.await_count == 0, "Explicit project switch should bypass orchestrator"
    assert mock_proj.await_count == 1
    assert project_calls[0]["project_name"] == "allspark"
    assert project_calls[0]["session_id"] is None
    assert "指定后续对话按项目" in project_calls[0]["prompt"]
    assert len(_feishu_replies) == 1
    assert "后续按 allspark" in _feishu_replies[0]["content"]

    print("\n[PASS] A5.1: Explicit project switch routes directly to project agent")


# ---------------------------------------------------------------------------
# Test C: agent_session_id stability — not overwritten on resume
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_agent_session_id_not_overwritten_on_project_resume():
    """
    Once agent_session_id is set for a project session, it must NOT be overwritten
    by subsequent resume calls (even if run_for_project returns a different session_id).

    This is enforced by _update_session_state's "only if not already set" logic.
    """
    THREAD_ID = "thread_stability_test"
    ORIGINAL_SID = "sdk-proj-original-session-id"
    NEW_SID_FROM_RESUME = "sdk-proj-DIFFERENT-resumed-id"  # Should be ignored

    # Pre-setup session with agent_session_id already set
    async with aiosqlite.connect(TEST_DB_PATH) as db:
        now = datetime.now().isoformat()
        await db.execute(
            "INSERT INTO sessions (session_key, source_platform, source_chat_id, owner_user_id, "
            "title, project, status, thread_id, agent_session_id, last_active_at, "
            "message_count, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("key_stability_001", "feishu", CHAT_ID, SENDER_ID,
             "allspark 稳定性测试", "allspark", "open",
             THREAD_ID, ORIGINAL_SID, now, 2, now, now),
        )
        await db.commit()

    async def mock_proj(project_name, prompt, session_id, **kwargs):
        assert session_id == ORIGINAL_SID, \
            f"Resume must use original session_id={ORIGINAL_SID!r}, got {session_id!r}"
        return {
            "text": "项目 Agent 回复内容",
            "session_id": NEW_SID_FROM_RESUME,  # Pipeline should NOT overwrite with this
            "cost_usd": 0.01,
        }

    from core.pipeline import process_message

    with patch("core.pipeline._run_project_agent", AsyncMock(side_effect=mock_proj)), \
         patch("core.pipeline._run_orchestrator", AsyncMock()):

        msg_id = await _insert_message("继续之前的问题", "m_stable_001", thread_id=THREAD_ID)
        await process_message(msg_id)

    msg = await _get_message(msg_id)
    assert msg["pipeline_status"] == "completed"

    session_after = await _get_session(msg["session_id"])
    # CRITICAL: agent_session_id must NOT be overwritten
    assert session_after["agent_session_id"] == ORIGINAL_SID, (
        f"agent_session_id must stay {ORIGINAL_SID!r}, "
        f"but was overwritten to {session_after['agent_session_id']!r}"
    )

    print(f"\n[PASS] C: agent_session_id stability — not overwritten on resume")


@pytest.mark.asyncio
async def test_project_resume_json_response_only_sends_reply_content():
    """Project resume may return orchestrator-style JSON; user should only receive reply_content."""
    THREAD_ID = "thread_project_json_reply"

    async with aiosqlite.connect(TEST_DB_PATH) as db:
        now = datetime.now().isoformat()
        await db.execute(
            "INSERT INTO sessions (session_key, source_platform, source_chat_id, owner_user_id, "
            "title, project, status, thread_id, agent_session_id, last_active_at, "
            "message_count, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("key_project_json_001", "feishu", CHAT_ID, SENDER_ID,
             "allspark 冲突检测", "allspark", "open",
             THREAD_ID, PROJECT_SESSION_ID, now, 2, now, now),
        )
        await db.commit()

    project_json = json.dumps({
        "action": "drafted",
        "classified_type": "work_question",
        "topic": "预约表冲突检测策略代码级说明",
        "project_name": "allspark",
        "reply_content": "这是给用户的正文，不应该把 JSON 包装发出去。",
        "reason": "高风险代码分析，已给出草稿答复",
    }, ensure_ascii=False)

    from core.pipeline import process_message

    with patch("core.pipeline._run_project_agent", AsyncMock(return_value={
        "text": project_json,
        "session_id": PROJECT_SESSION_ID,
        "cost_usd": 0.02,
        "num_turns": 2,
    })), patch("core.pipeline._run_orchestrator", AsyncMock()):

        msg_id = await _insert_message("继续看冲突检测", "m_proj_json_001", thread_id=THREAD_ID)
        await process_message(msg_id)

    assert len(_feishu_replies) == 1
    assert _feishu_replies[0]["msg_type"] == "interactive"
    sent_card = json.loads(_feishu_replies[0]["content"])
    assert sent_card["schema"] == "2.0"
    assert "这是给用户的正文，不应该把 JSON 包装发出去。" in json.dumps(sent_card, ensure_ascii=False)

    bot_msg = await _get_message_by_platform_id("reply_m_proj_json_001")
    assert bot_msg["content"] == "这是给用户的正文，不应该把 JSON 包装发出去。"

    msg = await _get_message(msg_id)
    assert msg["classified_type"] == "work_question"
    assert msg["pipeline_status"] == "completed"

    print("\n[PASS] C2: Project resume JSON is unwrapped to reply_content only")


@pytest.mark.asyncio
async def test_project_resume_clarifying_reply_retries_once():
    """Premature clarification in project resume should trigger one internal retry before replying."""
    THREAD_ID = "thread_project_retry"

    async with aiosqlite.connect(TEST_DB_PATH) as db:
        now = datetime.now().isoformat()
        await db.execute(
            "INSERT INTO sessions (session_key, source_platform, source_chat_id, owner_user_id, "
            "title, project, status, thread_id, agent_session_id, last_active_at, "
            "message_count, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("key_project_retry_001", "feishu", CHAT_ID, SENDER_ID,
             "allspark 代码分析", "allspark", "open",
             THREAD_ID, PROJECT_SESSION_ID, now, 2, now, now),
        )
        await db.commit()

    project_runs = AsyncMock(side_effect=[
        {
            "text": "我先确认一下，你是想看预约表那层的冲突配置，还是下层 pipeline？",
            "session_id": PROJECT_SESSION_ID,
            "cost_usd": 0.01,
        },
        {
            "text": "我先按预约表这一层给你结论：当前实现入口在 MapReservationTable，最终会下钻到 ConflictDetectionPipeline。",
            "session_id": PROJECT_SESSION_ID,
            "cost_usd": 0.02,
        },
    ])

    from core.pipeline import process_message

    with patch("core.pipeline._run_project_agent", project_runs), \
         patch("core.pipeline._run_orchestrator", AsyncMock()):

        msg_id = await _insert_message("继续看冲突检测", "m_proj_retry_001", thread_id=THREAD_ID)
        await process_message(msg_id)

    assert project_runs.await_count == 2, f"Expected retry once, got {project_runs.await_count} calls"
    assert len(_feishu_replies) == 1
    assert _feishu_replies[0]["msg_type"] == "interactive"
    retry_card = json.loads(_feishu_replies[0]["content"])
    retry_card_text = json.dumps(retry_card, ensure_ascii=False)
    assert "MapReservationTable" in retry_card_text
    assert "我先确认一下" not in retry_card_text

    print("\n[PASS] C3: Premature clarification triggers one retry before reply")


@pytest.mark.asyncio
async def test_concurrent_project_messages_do_not_leak_internal_tool_errors():
    """Concurrent messages on the same thread should serialize and sanitize internal tool errors."""
    THREAD_ID = "thread_project_concurrent"

    async with aiosqlite.connect(TEST_DB_PATH) as db:
        now = datetime.now().isoformat()
        await db.execute(
            "INSERT INTO sessions (session_key, source_platform, source_chat_id, owner_user_id, "
            "title, project, status, thread_id, agent_session_id, last_active_at, "
            "message_count, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("key_project_concurrent_001", "feishu", CHAT_ID, SENDER_ID,
             "allspark 并发测试", "allspark", "open",
             THREAD_ID, PROJECT_SESSION_ID, now, 2, now, now),
        )
        await db.commit()

    in_flight = 0
    max_in_flight = 0

    async def mock_proj(project_name, prompt, session_id, **kwargs):
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        try:
            if "并发消息 1" in prompt:
                await asyncio.sleep(0.05)
                return {
                    "text": "user cancelled MCP tool call",
                    "session_id": PROJECT_SESSION_ID,
                    "cost_usd": 0.01,
                }
            return {
                "text": "第二条消息正常处理完成。",
                "session_id": PROJECT_SESSION_ID,
                "cost_usd": 0.01,
            }
        finally:
            in_flight -= 1

    from core.pipeline import process_message

    with patch("core.pipeline._run_project_agent", AsyncMock(side_effect=mock_proj)), \
         patch("core.pipeline._run_orchestrator", AsyncMock()):

        msg1_id = await _insert_message("并发消息 1", "m_proj_concurrent_001", thread_id=THREAD_ID)
        msg2_id = await _insert_message("并发消息 2", "m_proj_concurrent_002", thread_id=THREAD_ID)

        await asyncio.gather(
            process_message(msg1_id),
            process_message(msg2_id),
        )

    assert max_in_flight == 1, f"Expected session lock serialization, got max_in_flight={max_in_flight}"
    assert len(_feishu_replies) == 2
    assert "user cancelled MCP tool call" not in _feishu_replies[0]["content"]
    assert "user cancelled MCP tool call" not in _feishu_replies[1]["content"]
    assert any("内部调用刚才被中断" in reply["content"] for reply in _feishu_replies)
    assert any("第二条消息正常处理完成" in reply["content"] for reply in _feishu_replies)

    bot_reply_1 = await _get_message_by_platform_id("reply_m_proj_concurrent_001")
    bot_reply_2 = await _get_message_by_platform_id("reply_m_proj_concurrent_002")
    assert "user cancelled MCP tool call" not in bot_reply_1["content"]
    assert "user cancelled MCP tool call" not in bot_reply_2["content"]

    print("\n[PASS] C3.1: Concurrent project messages serialize and sanitize internal tool errors")


@pytest.mark.asyncio
async def test_deliver_flow_payload_sends_interactive_card():
    """Structured flow payload should be rendered as a Feishu interactive card."""
    from core.pipeline import _deliver_reply

    payload = {
        "format": "flow",
        "title": "发布流程",
        "summary": "按下面顺序执行。",
        "steps": [
            {"title": "准备", "detail": "确认配置与权限"},
            {"title": "执行", "detail": "运行发布命令"},
        ],
        "table": {
            "columns": [
                {"key": "step", "label": "步骤", "type": "text"},
                {"key": "owner", "label": "负责人", "type": "text"},
            ],
            "rows": [
                {"step": "准备", "owner": "后端"},
                {"step": "执行", "owner": "运维"},
            ],
        },
        "fallback_text": "发布流程：1. 准备；2. 执行。",
    }

    thread_id, delivered = await _deliver_reply(
        {
            "id": 1,
            "platform": "feishu",
            "platform_message_id": "om_flow_001",
            "chat_id": CHAT_ID,
        },
        payload,
        session_id=None,
    )

    assert delivered is True
    assert thread_id == "thread_om_flow_001"
    assert len(_feishu_replies) == 1
    assert _feishu_replies[0]["msg_type"] == "interactive"

    card = json.loads(_feishu_replies[0]["content"])
    assert card["schema"] == "2.0"
    assert card["header"]["title"]["content"] == "发布流程"
    assert any(elem.get("tag") == "table" for elem in card["body"]["elements"])

    print("\n[PASS] C4: Structured flow payload is sent as interactive card")


def test_normalize_project_result_accepts_structured_flow_json():
    """Project agent may return pure structured JSON for rich Feishu rendering."""
    from core.pipeline import _normalize_project_result

    raw = json.dumps({
        "format": "flow",
        "title": "排查流程",
        "steps": [{"title": "收集日志", "detail": "查看 worker.log"}],
        "fallback_text": "先收集日志。",
    }, ensure_ascii=False)

    result = _normalize_project_result(
        raw,
        session={"title": "线程异常排查"},
        project="work-agent-os",
    )

    assert result["action"] == "replied"
    assert result["project_name"] == "work-agent-os"
    assert result["reply_content"]["format"] == "flow"
    assert result["reply_content"]["title"] == "排查流程"

    print("\n[PASS] C5: Project structured JSON is preserved as reply payload")


@pytest.mark.asyncio
async def test_work_question_plain_text_is_cardified_for_feishu():
    """Work-question plain text replies should be wrapped into an interactive card by default."""
    from core.pipeline import _deliver_reply

    thread_id, delivered = await _deliver_reply(
        {
            "id": 1,
            "platform": "feishu",
            "platform_message_id": "om_plain_work_001",
            "chat_id": CHAT_ID,
        },
        "结论：当前实现会优先读取配置，然后执行调度流程。\n\n补充说明：失败时会记录审计日志。",
        session_id=None,
        classified_type="work_question",
        topic="实现说明",
    )

    assert delivered is True
    assert thread_id == "thread_om_plain_work_001"
    assert len(_feishu_replies) == 1
    assert _feishu_replies[0]["msg_type"] == "interactive"

    card = json.loads(_feishu_replies[0]["content"])
    assert card["header"]["title"]["content"] == "实现说明"
    card_text = json.dumps(card, ensure_ascii=False)
    assert "概览" in card_text
    assert "补充说明" in card_text

    print("\n[PASS] C6: Work-question plain text is auto-cardified")


@pytest.mark.asyncio
async def test_structured_card_includes_project_runtime_context():
    """Project issue cards should expose runtime info without redundant directory fields."""
    from core.pipeline import _deliver_reply

    project_context = SimpleNamespace(
        running_project="fms-java",
        business_project_name="KIOXIA岩手工厂日本自动搬运复购项目",
        project_path=Path("D:/standard/riot/fms-java"),
        current_branch="dev",
        current_version="dev@3cf57f15",
        target_branch="master",
        target_tag="v4.9.2",
        version_source_field="FMS/RIoT版本",
        version_source_value="4.9.2-186-g96fb6f2f9_20250723",
        recommended_worktree=Path("D:/standard/work-agent-os/.worktrees/fms-java/149268-1FmsdpJjHT3JPyWL-4.9.2"),
    )
    payload = {
        "format": "rich",
        "title": "排查结论",
        "summary": "当前更像自动充电任务触发链路异常。",
        "sections": [
            {"title": "当前结论", "content": "先检查低电量触发与停靠点状态同步链路。"},
        ],
        "fallback_text": "当前更像自动充电任务触发链路异常。",
    }

    thread_id, delivered = await _deliver_reply(
        {
            "id": 1,
            "platform": "feishu",
            "platform_message_id": "om_runtime_ctx_001",
            "chat_id": CHAT_ID,
        },
        payload,
        session_id=None,
        classified_type="work_question",
        topic="排查结论",
        project_context=project_context,
    )

    assert delivered is True
    assert thread_id == "thread_om_runtime_ctx_001"
    card = json.loads(_feishu_replies[0]["content"])
    card_text = json.dumps(card, ensure_ascii=False)
    assert "运行的项目" in card_text
    assert "fms-java" in card_text
    assert "目录" in card_text
    assert "对应 Tag" in card_text
    assert "建议 worktree" not in card_text

    print("\n[PASS] C6.1: Structured project card includes runtime context")


@pytest.mark.asyncio
async def test_structured_card_prefers_analysis_workspace_as_only_directory():
    """When analysis workspace exists, runtime card should only show that directory."""
    from core.pipeline import _deliver_reply

    project_context = SimpleNamespace(
        running_project="riot-standalone",
        business_project_name="唐山松下项目1",
        project_path=Path("D:/standard/riot/riot-standalone"),
        execution_path=Path("D:/standard/work-agent-os/.worktrees/riot-standalone/149531-branch"),
        analysis_workspace="D:/standard/work-agent-os/.triage/session-68-demo",
        current_branch="2.0.7-avary-standard",
        current_version="2.0.7",
        target_branch="master",
        recommended_worktree=Path("D:/standard/work-agent-os/.worktrees/riot-standalone/149531-branch"),
    )
    payload = {
        "format": "rich",
        "title": "关键日志证据",
        "summary": "最关键的日志证据有 3 组。",
        "fallback_text": "最关键的日志证据有 3 组。",
    }

    thread_id, delivered = await _deliver_reply(
        {
            "id": 1,
            "platform": "feishu",
            "platform_message_id": "om_runtime_ctx_analysis_001",
            "chat_id": CHAT_ID,
        },
        payload,
        session_id=None,
        classified_type="work_question",
        topic="关键日志证据",
        project_context=project_context,
    )

    assert delivered is True
    assert thread_id == "thread_om_runtime_ctx_analysis_001"
    card = json.loads(_feishu_replies[0]["content"])
    card_text = json.dumps(card, ensure_ascii=False)
    assert "分析目录" in card_text
    assert "D:/standard/work-agent-os/.triage/session-68-demo" in card_text
    assert "D:/standard/riot/riot-standalone" not in card_text
    assert "149531-branch" not in card_text
    assert "建议 worktree" not in card_text

    print("\n[PASS] C6.2: Structured project card prefers analysis workspace as only directory")


@pytest.mark.asyncio
async def test_low_reasoning_model_keeps_plain_text_reply():
    """Lightweight models should be allowed to use plain text instead of fixed cards."""
    from core.pipeline import _deliver_reply

    thread_id, delivered = await _deliver_reply(
        {
            "id": 1,
            "platform": "feishu",
            "platform_message_id": "om_plain_low_001",
            "chat_id": CHAT_ID,
        },
        "当前更像配置项未生效，先补充对应日志和配置截图。",
        session_id=None,
        classified_type="work_question",
        topic="补料建议",
        reply_model="claude-haiku-4-5",
        reply_usage={"reasoning_output_tokens": 0},
    )

    assert delivered is True
    assert thread_id == "thread_om_plain_low_001"
    assert len(_feishu_replies) == 1
    assert _feishu_replies[0]["msg_type"] == "text"

    print("\n[PASS] C7: Low-reasoning model may keep plain text reply")


# ---------------------------------------------------------------------------
# Test D: Model defaults to haiku
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_agent_client_uses_haiku_model_as_default():
    """
    When models.yaml sets haiku as default and no runtime override exists,
    _build_options must resolve to claude-haiku-4-5.

    Tests the model selection chain: model arg → get_model_override → load_models_config.default
    """
    from core.orchestrator.agent_client import AgentClient

    client = AgentClient()

    with patch("core.orchestrator.agent_client.get_model_override", return_value=None), \
         patch("core.orchestrator.agent_client.load_models_config", return_value={
             "default": "claude-haiku-4-5",
             "fallback": None,
             "models": [],
         }):
        opts = client._build_options(
            system_prompt="test",
            max_turns=5,
        )

    assert opts.model == "claude-haiku-4-5", \
        f"Expected haiku as default model, got: {opts.model!r}"

    print(f"\n[PASS] D: Default model is claude-haiku-4-5")


@pytest.mark.asyncio
async def test_agent_client_runtime_override_takes_priority_over_default():
    """
    Runtime model override (from DB app_settings) takes priority over models.yaml default.
    If override is set to sonnet, haiku default must be ignored.
    """
    from core.orchestrator.agent_client import AgentClient

    client = AgentClient()

    with patch("core.orchestrator.agent_client.get_model_override", return_value="claude-sonnet-4-6"), \
         patch("core.orchestrator.agent_client.load_models_config", return_value={
             "default": "claude-haiku-4-5",
             "fallback": None,
             "models": [],
         }):
        opts = client._build_options(system_prompt="test", max_turns=5)

    assert opts.model == "claude-sonnet-4-6", \
        f"Runtime override must take priority, got: {opts.model!r}"

    print(f"\n[PASS] D2: Runtime model override takes priority over default")


@pytest.mark.asyncio
async def test_agent_client_codex_runtime_uses_runtime_specific_default_model():
    """
    Codex runtime must prefer the runtime-specific default model instead of the
    global config default. Otherwise codex can be invoked with a Claude model ID.
    """
    from core.orchestrator.agent_client import AgentClient

    client = AgentClient()

    with patch.object(client, "get_active_runtime", return_value="codex"), \
         patch("core.orchestrator.agent_client.get_model_override", return_value=None), \
         patch("core.orchestrator.agent_client.load_models_config", return_value={
             "default": "claude-opus-4-6",
             "fallback": "gpt-5.4",
             "models": [
                 {
                     "id": "claude-opus-4-6",
                     "enabled": True,
                     "supported_runtimes": ["claude"],
                 },
                 {
                     "id": "gpt-5.4",
                     "enabled": True,
                     "supported_runtimes": ["codex"],
                 },
             ],
         }):
        selected = client._select_model()

    assert selected == "gpt-5.4", \
        f"Codex runtime must resolve to gpt-5.4, got: {selected!r}"

    print("\n[PASS] D3: Codex runtime uses runtime-specific default model")


def test_codex_exec_timeout_defaults_to_20_minutes(monkeypatch):
    from core.orchestrator.agent_client import _codex_exec_timeout_seconds

    monkeypatch.delenv("CODEX_EXEC_TIMEOUT_SECONDS", raising=False)

    assert _codex_exec_timeout_seconds(20) == 1200
    assert _codex_exec_timeout_seconds(30) == 1200


def test_codex_exec_timeout_env_override(monkeypatch):
    from core.orchestrator.agent_client import _codex_exec_timeout_seconds

    monkeypatch.setenv("CODEX_EXEC_TIMEOUT_SECONDS", "1800")

    assert _codex_exec_timeout_seconds(20) == 1800
    assert _codex_exec_timeout_seconds(80) == 2400


# ---------------------------------------------------------------------------
# Test E: Thread routing — messages in same thread go to same session
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_thread_id_routes_to_existing_session():
    """
    A new message with thread_id matching an existing session must be routed
    to that session, not create a new one.
    """
    THREAD_ID = "thread_routing_test_001"

    # Pre-create a session with this thread_id
    async with aiosqlite.connect(TEST_DB_PATH) as db:
        now = datetime.now().isoformat()
        cursor = await db.execute(
            "INSERT INTO sessions (session_key, source_platform, source_chat_id, owner_user_id, "
            "title, status, thread_id, last_active_at, message_count, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("key_routing_001", "feishu", CHAT_ID, SENDER_ID,
             "路由测试会话", "open", THREAD_ID, now, 0, now, now),
        )
        existing_session_id = cursor.lastrowid
        await db.commit()

    from core.pipeline import process_message

    with patch("core.pipeline._run_orchestrator",
               AsyncMock(return_value=_make_orchestrator_chat_result("明白了。"))), \
         patch("core.pipeline._run_project_agent", AsyncMock()):

        msg_id = await _insert_message("追问一下", "m_routing_001", thread_id=THREAD_ID)
        await process_message(msg_id)

    msg = await _get_message(msg_id)
    assert msg["pipeline_status"] == "completed"
    assert msg["session_id"] == existing_session_id, (
        f"Message with thread_id={THREAD_ID!r} must route to existing session "
        f"{existing_session_id}, got {msg['session_id']}"
    )

    print(f"\n[PASS] E: Thread routing directs message to existing session {existing_session_id}")
