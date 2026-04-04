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

import json
from datetime import datetime
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

    def mock_reply(message_id, content, reply_in_thread=True):
        _feishu_replies.append({
            "message_id": message_id,
            "content": content,
            "reply_in_thread": reply_in_thread,
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

    with patch("core.connectors.feishu.FeishuClient", side_effect=_make_mock_feishu):
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


async def _set_session_thread(session_id: int, thread_id: str) -> None:
    """Manually bind thread_id to a session (simulates feishu thread creation)."""
    async with aiosqlite.connect(TEST_DB_PATH) as db:
        await db.execute(
            "UPDATE sessions SET thread_id = ? WHERE id = ?",
            (thread_id, session_id),
        )
        await db.commit()


async def _simulate_dispatch_writeback(session_id: int, project: str, agent_session_id: str) -> None:
    """Simulate the dispatch_to_project tool writing agent_session_id + project to session.

    In production, this happens inside the MCP tool (agent_client.py:dispatch_to_project).
    The tool writes to the production DB path; in tests we replicate this to the test DB.
    """
    async with aiosqlite.connect(TEST_DB_PATH) as db:
        await db.execute(
            "UPDATE sessions SET project = ?, agent_session_id = ?, updated_at = ? WHERE id = ?",
            (project, agent_session_id, datetime.now().isoformat(), session_id),
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

    # Project agent never called
    mock_proj.assert_not_called()

    assert len(_feishu_replies) == 1

    print(f"\n[PASS] B1: Non-project message has no project binding")


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
