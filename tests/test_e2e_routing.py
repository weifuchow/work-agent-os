"""Real-API end-to-end routing tests.

Mocks ONLY: FeishuClient (send/receive simulation)
Real:        Claude API (haiku), pipeline, orchestrator, dispatch_to_project tool,
             project agent resume, DB writes

飞书话题（thread）模拟流程：
  Turn 1: 用户发原始消息（无 thread_id）
          Bot 回复 → 飞书创建话题 → 返回 thread_id
          Pipeline 将 thread_id 写入 session
  Turn 2+: 用户在话题内回复（incoming 消息携带 thread_id）
           Pipeline 通过 thread_id 路由到同一 session
           → 有 agent_session_id+project → 直接 resume 项目 Agent
           → 无 project → orchestrator（新 turn）

Scenario A — Project (allspark), 3 turns:
  1. allspark 是什么          ← 无 thread_id（新对话）
  2. 简单项目支持的数据源      ← 携带 thread_id（话题内追问）
  3. 这个项目 sqlserver 怎么配置 ← 携带 thread_id（话题内继续）

Scenario B — Non-project (music), 3 turns:
  1. radiohead 和 queen 乐队对比 ← 无 thread_id
  2. radiohead 什么歌好听        ← 携带 thread_id
  3. 分析一下他们为什么抓人      ← 携带 thread_id

Success criteria:
  A: Turn 1 → session.project=="allspark", agent_session_id 非空
     Turn 2/3 → agent_session_id 与 Turn 1 完全一致（claude --resume 正确 ID）
     全程同一 DB session，3 次飞书回复
  B: 全程 session.project==""
     agent_session_id 在 Turn 1 后设定，Turn 2/3 保持不变（只写一次保证）
     全程同一 DB session，3 次飞书回复

Run:
    pytest tests/test_e2e_routing.py -v -s -m e2e

Note: each turn calls real Claude API (~10-30s). Total ~3-5 minutes.
"""

import uuid
from datetime import datetime
from unittest.mock import MagicMock, patch, AsyncMock

import aiosqlite
import pytest

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Unique prefix so test data is identifiable in production DB
TEST_CHAT_ID = "oc_e2e_routing_test"
TEST_SENDER_ID = "ou_e2e_tester"
TEST_SENDER_NAME = "E2E Tester"

# Use the system's current configured model (no override)
# Real e2e tests use whatever is set in app_settings / models.yaml

PROJECT_ROOT_PATH = __import__("pathlib").Path(__file__).resolve().parent.parent
DB_PATH = str(PROJECT_ROOT_PATH / "data" / "db" / "app.sqlite")

# ---------------------------------------------------------------------------
# Feishu mock — simulates send/reply, returns deterministic thread_id
# ---------------------------------------------------------------------------

class FakeFeishuClient:
    """Simulates Feishu message send/reply with realistic thread behavior.

    Thread model (mirrors real Feishu):
      - First reply_message call creates the thread → returns a new thread_id
      - All subsequent replies in the same conversation reuse that thread_id
        (Feishu话题：一旦创建 thread_id 不变)
      - Incoming messages must carry this thread_id to be routed to same session
    """

    def __init__(self):
        self.replies: list[dict] = []
        self._thread_id: str | None = None  # stable for the lifetime of this client

    def reply_message(self, message_id: str, content: str, reply_in_thread: bool = True) -> dict:
        if self._thread_id is None:
            # First bot reply creates the Feishu thread
            self._thread_id = f"e2e_thread_{message_id}"

        self.replies.append({
            "message_id": message_id,
            "content": content,
            "thread_id": self._thread_id,
        })
        return {"message_id": f"bot_{message_id}", "thread_id": self._thread_id}

    def send_message(self, receive_id: str, content: str, **kwargs) -> dict:
        return {"message_id": f"sent_{uuid.uuid4().hex[:8]}"}


# Each test gets its own client instance to track replies independently
_feishu_client: FakeFeishuClient | None = None

def _feishu_factory():
    return _feishu_client


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

async def _insert_msg(content: str, thread_id: str = "") -> int:
    now = datetime.now().isoformat()
    mid = f"e2e_{uuid.uuid4().hex[:10]}"
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO messages "
            "(platform, platform_message_id, chat_id, sender_id, sender_name, "
            "message_type, content, received_at, raw_payload, "
            "thread_id, root_id, parent_id, pipeline_status, pipeline_error, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("feishu", mid, TEST_CHAT_ID, TEST_SENDER_ID, TEST_SENDER_NAME,
             "text", content, now, "",
             thread_id, "", "", "pending", "", now),
        )
        await db.commit()
        return cursor.lastrowid


async def _get_msg(msg_id: int) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM messages WHERE id = ?", (msg_id,))
        row = await cursor.fetchone()
        return dict(row) if row else {}


async def _get_session(session_id: int) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,))
        row = await cursor.fetchone()
        return dict(row) if row else {}


async def _get_bot_reply(platform_message_id: str) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT content FROM messages WHERE platform_message_id = ?",
            (f"reply_{platform_message_id}",),
        )
        row = await cursor.fetchone()
        return row[0] if row else ""


async def _cleanup_test_data():
    """Remove all messages and sessions created by this test run."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM session_messages WHERE session_id IN "
                         "(SELECT id FROM sessions WHERE source_chat_id = ?)", (TEST_CHAT_ID,))
        await db.execute("DELETE FROM messages WHERE chat_id = ?", (TEST_CHAT_ID,))
        await db.execute("DELETE FROM sessions WHERE source_chat_id = ?", (TEST_CHAT_ID,))
        await db.commit()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
async def e2e_setup():
    global _feishu_client
    _feishu_client = FakeFeishuClient()
    await _cleanup_test_data()  # clean up any previous test run first

    with patch("core.connectors.feishu.FeishuClient", side_effect=_feishu_factory):
        yield

    await _cleanup_test_data()


# ---------------------------------------------------------------------------
# Turn helper
# ---------------------------------------------------------------------------

async def _run_turn(
    turn_no: int,
    content: str,
    thread_id: str = "",
) -> dict:
    """Insert + process one message. Returns state dict.

    thread_id:
      Turn 1 → "" (新对话，飞书原始消息无话题)
      Turn 2+ → 上一轮 reply 返回的 thread_id（用户在飞书话题内回复）
    """
    from core.pipeline import process_message

    thread_label = f"thread={thread_id!r}" if thread_id else "无 thread（新对话）"
    print(f"\n  ── Turn {turn_no} [{thread_label}] ──")
    print(f"  User: {content!r}")

    msg_id = await _insert_msg(content, thread_id=thread_id)
    await process_message(msg_id)

    msg = await _get_msg(msg_id)
    status = msg.get("pipeline_status", "?")
    session_id = msg.get("session_id")
    classified = msg.get("classified_type", "?")

    assert status == "completed", (
        f"Turn {turn_no} failed: status={status!r} "
        f"error={msg.get('pipeline_error', '')[:200]}"
    )

    session = await _get_session(session_id) if session_id else {}
    reply = await _get_bot_reply(msg["platform_message_id"])

    # thread_id 从 session 读取（pipeline 在首次 reply 后写入）
    session_thread = session.get("thread_id", "")
    print(f"  Bot [{session_thread!r}]: {reply[:200]}{'...' if len(reply) > 200 else ''}")
    print(f"  session={session_id}  classified={classified}  "
          f"project={session.get('project', '')!r}  "
          f"agent_session_id={session.get('agent_session_id', '')!r}")

    return {
        "msg_id": msg_id,
        "platform_message_id": msg["platform_message_id"],
        "session_id": session_id,
        "thread_id": session_thread,          # 来自 session（pipeline 写入）
        "feishu_thread_id": _feishu_client._thread_id,  # 来自 FakeFeishuClient（话题 ID）
        "agent_session_id": session.get("agent_session_id", ""),
        "project": session.get("project", ""),
        "classified_type": classified,
        "reply": reply,
    }


# ---------------------------------------------------------------------------
# Scenario A: Project (allspark) — 3 turns
# ---------------------------------------------------------------------------

@pytest.mark.e2e
@pytest.mark.asyncio
async def test_scenario_a_project_allspark_routing():
    """
    3-turn allspark conversation.

    Turn 1: Orchestrator must identify allspark → call dispatch_to_project tool
            → dispatch writes agent_session_id + project to session
    Turn 2: Pipeline detects agent_session_id+project → _run_project_agent(resume)
    Turn 3: Same, resume continues

    Key assertions:
      - session.project == "allspark" after turn 1
      - session.agent_session_id is set and non-empty after turn 1
      - Turns 2 and 3 use the same agent_session_id (resume, not new session)
      - All 3 turns share the same DB session
    """
    print("\n" + "=" * 60)
    print("Scenario A: Project routing (allspark)")
    print("=" * 60)

    # Turn 1
    t1 = await _run_turn(1, "allspark 是什么")
    session_id = t1["session_id"]
    thread_id = t1["thread_id"]

    assert t1["project"] == "allspark", (
        f"Turn 1: expected project='allspark', got {t1['project']!r}\n"
        f"Orchestrator did not route to allspark. Check system prompt project list."
    )
    assert t1["agent_session_id"], (
        "Turn 1: agent_session_id not set after dispatch.\n"
        "dispatch_to_project tool may not have written back to session."
    )
    assert t1["reply"], "Turn 1: no reply content saved to DB"

    agent_sid_after_t1 = t1["agent_session_id"]
    print(f"\n  ✓ Turn 1 routed to allspark, agent_session_id={agent_sid_after_t1!r}")

    # Turn 2 — same thread, should resume project agent
    assert thread_id, "No thread_id after turn 1 — pipeline must bind thread_id on first reply"
    t2 = await _run_turn(2, "简单项目支持的数据源", thread_id=thread_id)

    assert t2["session_id"] == session_id, \
        f"Turn 2 landed in different session: {t2['session_id']} vs {session_id}"
    assert t2["project"] == "allspark"
    assert t2["agent_session_id"] == agent_sid_after_t1, (
        f"Turn 2: agent_session_id changed!\n"
        f"  Expected (resume): {agent_sid_after_t1!r}\n"
        f"  Got:               {t2['agent_session_id']!r}\n"
        f"This means `claude --resume` used the wrong session."
    )

    print(f"  ✓ Turn 2 resumed project agent (same agent_session_id)")

    # Turn 3 — same thread, continue resume
    t3 = await _run_turn(3, "这个项目 sqlserver 怎么配置", thread_id=thread_id)

    assert t3["session_id"] == session_id, \
        f"Turn 3 landed in different session: {t3['session_id']} vs {session_id}"
    assert t3["agent_session_id"] == agent_sid_after_t1, (
        f"Turn 3: agent_session_id changed.\n"
        f"  Expected: {agent_sid_after_t1!r}\n"
        f"  Got:      {t3['agent_session_id']!r}"
    )
    assert t3["reply"]

    print(f"  ✓ Turn 3 resumed project agent (same agent_session_id)")

    # --- Summary ---
    print(f"\n{'─' * 60}")
    print(f"  session_id       = {session_id}")
    print(f"  thread_id        = {thread_id!r}")
    print(f"  project          = 'allspark'")
    print(f"  agent_session_id = {agent_sid_after_t1!r} (stable across all 3 turns)")
    print(f"  feishu replies   = {len(_feishu_client.replies)}")
    assert len(_feishu_client.replies) == 3, \
        f"Expected 3 feishu replies (one per turn), got {len(_feishu_client.replies)}"
    print(f"\n  ✅ Scenario A PASSED — project routing + resume verified")


# ---------------------------------------------------------------------------
# Scenario B: Non-project (music) — 3 turns
# ---------------------------------------------------------------------------

@pytest.mark.e2e
@pytest.mark.asyncio
async def test_scenario_b_non_project_music():
    """
    3-turn music discussion — no project association.

    Thread flow:
      Turn 1: no thread_id → orchestrator replies → FakeFeishuClient creates thread
      Turn 2: thread_id from Turn 1 reply → routes to same session → orchestrator again
      Turn 3: same thread_id → same session → orchestrator again

    Key assertions:
      - session.project == "" throughout (orchestrator never dispatched)
      - agent_session_id set after Turn 1 (orchestrator's SDK session)
      - agent_session_id stable across Turn 2/3 (only-write-once invariant)
      - All 3 turns in same DB session via thread_id routing
      - 3 feishu replies (one per turn, no duplicates)
    """
    print("\n" + "=" * 60)
    print("Scenario B: Non-project routing (music)")
    print("=" * 60)

    # Turn 1 — 新对话，无 thread_id
    t1 = await _run_turn(1, "radiohead 和 queen 乐队对比")
    session_id = t1["session_id"]

    assert t1["project"] == "", (
        f"Turn 1: got unexpected project binding {t1['project']!r} for a music question."
    )
    assert t1["reply"], "Turn 1: no reply content"

    # thread_id 在首次回复后由 pipeline 写入 session
    thread_id = t1["thread_id"]
    assert thread_id, (
        "No thread_id after Turn 1.\n"
        "Pipeline must bind thread_id from FakeFeishuClient reply to session."
    )
    # FakeFeishuClient 与 session 的 thread_id 必须一致
    assert thread_id == t1["feishu_thread_id"], (
        f"session.thread_id={thread_id!r} != feishu.thread_id={t1['feishu_thread_id']!r}"
    )

    agent_sid = t1["agent_session_id"]
    assert agent_sid, "Turn 1: agent_session_id not set (orchestrator should set it)"
    print(f"  ✓ Turn 1: project='', thread_id={thread_id!r}, agent_session_id={agent_sid!r}")

    # Turn 2 — 用户在飞书话题内回复（携带 thread_id）
    t2 = await _run_turn(2, "radiohead 什么歌好听", thread_id=thread_id)

    assert t2["session_id"] == session_id, \
        f"Turn 2 routed to different session: {t2['session_id']} vs {session_id}"
    assert t2["project"] == "", \
        f"Turn 2 unexpectedly dispatched to project={t2['project']!r}"
    # agent_session_id 只写一次，Turn 2 不应覆盖
    assert t2["agent_session_id"] == agent_sid, (
        f"Turn 2: agent_session_id changed!\n"
        f"  Expected (stable): {agent_sid!r}\n"
        f"  Got:               {t2['agent_session_id']!r}\n"
        f"_update_session_state 'only-write-once' invariant violated."
    )
    assert t2["reply"]
    print(f"  ✓ Turn 2: same session, project='', agent_session_id stable")

    # Turn 3 — 继续在同一话题内
    t3 = await _run_turn(3, "分析一下他们为什么抓人", thread_id=thread_id)

    assert t3["session_id"] == session_id, \
        f"Turn 3 routed to different session: {t3['session_id']} vs {session_id}"
    assert t3["project"] == "", \
        f"Turn 3 unexpectedly dispatched to project={t3['project']!r}"
    assert t3["agent_session_id"] == agent_sid, (
        f"Turn 3: agent_session_id changed to {t3['agent_session_id']!r}"
    )
    assert t3["reply"]
    print(f"  ✓ Turn 3: same session, project='', agent_session_id stable")

    # --- Summary ---
    print(f"\n{'─' * 60}")
    print(f"  session_id       = {session_id}")
    print(f"  thread_id        = {thread_id!r}")
    print(f"  project          = '' (empty, all 3 turns)")
    print(f"  agent_session_id = {agent_sid!r} (stable, set once)")
    print(f"  feishu replies   = {len(_feishu_client.replies)}")
    assert len(_feishu_client.replies) == 3, \
        f"Expected 3 feishu replies, got {len(_feishu_client.replies)}"
    print(f"\n  ✅ Scenario B PASSED — non-project routing + session continuity verified")
