"""End-to-end integration test for multi-turn session routing.

Validates that 3 messages in the same chat produce:
1. messages table — 3 user msgs + 3 bot replies, all with same session_id
2. sessions table — exactly 1 session with correct message_count
3. session_messages — 6 records (3 user + 3 assistant) in sequence
4. task_contexts — 1 task context linked to the session
5. conversations API — 3 user-bot pairs
6. audit_logs — pipeline_agent_call + pipeline_agent_result for each round
7. session_context injected into prompt for rounds 2 & 3
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel

import models.db  # noqa: F401
from apps.api.main import app
from core.orchestrator import agent_client as ac_module
from core.pipeline import process_message

CHAT_ID = "oc_test_multiturn_001"
SENDER_ID = "ou_test_user_001"

ROUNDS = [
    {
        "content": "帮我分析一下登录模块的性能问题",
        "reply": "登录模块的性能瓶颈主要在数据库查询和 token 生成两个环节。",
        "topic": "登录模块性能分析",
    },
    {
        "content": "具体是哪个接口最慢",
        "reply": "POST /api/auth/login 接口平均耗时 800ms，主要是用户查询未走索引。",
        "topic": "登录模块性能分析",
    },
    {
        "content": "用缓存优化一下这个接口",
        "reply": "建议对用户查询结果加 Redis 缓存，TTL 设为 5 分钟，预计可降低到 100ms。",
        "topic": "登录模块性能分析",
    },
]

# Will be set by fixture
_test_db_path: str = ""


async def _insert_message(round_idx: int) -> int:
    """Insert a simulated user message via aiosqlite. Returns message id."""
    import aiosqlite
    from datetime import datetime, UTC

    r = ROUNDS[round_idx]
    now = datetime.now(UTC).isoformat()
    pid = f"test_msg_{round_idx:03d}"

    async with aiosqlite.connect(_test_db_path) as db:
        cursor = await db.execute("SELECT id FROM messages WHERE platform_message_id = ?", (pid,))
        existing = await cursor.fetchone()
        if existing:
            return existing[0]

        await db.execute(
            "INSERT INTO messages (platform, platform_message_id, chat_id, sender_id, "
            "sender_name, message_type, content, received_at, raw_payload, "
            "pipeline_status, pipeline_error, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("feishu", pid, CHAT_ID, SENDER_ID, "TestUser", "text",
             r["content"], now, "", "pending", "", now),
        )
        await db.commit()
        cursor = await db.execute("SELECT last_insert_rowid()")
        return (await cursor.fetchone())[0]


def _make_mock_agent_run():
    """Mock agent_client.run — simulates Orchestrator calling route_to_session."""
    call_count = 0
    tc_id = None

    async def mock_run(prompt: str, system_prompt: str = "", max_turns: int = 30,
                       session_id=None, skill=None):
        nonlocal call_count, tc_id
        idx = call_count
        call_count += 1
        r = ROUNDS[idx]

        # Extract message_id from prompt
        msg_id = None
        for line in prompt.splitlines():
            if "消息ID" in line:
                msg_id = int(line.replace("：", ":").split(":")[-1].strip())
                break

        import aiosqlite
        from datetime import datetime, UTC, timedelta

        async with aiosqlite.connect(_test_db_path) as db:
            db.row_factory = aiosqlite.Row
            now = datetime.now(UTC).isoformat()
            cutoff = (datetime.now(UTC) - timedelta(hours=2)).isoformat()

            cursor = await db.execute(
                "SELECT id FROM sessions WHERE source_chat_id = ? AND status IN ('open','waiting') "
                "AND last_active_at >= ? ORDER BY last_active_at DESC LIMIT 1",
                (CHAT_ID, cutoff),
            )
            row = await cursor.fetchone()

            if row:
                sess_id = row["id"]
                cursor2 = await db.execute("SELECT message_count FROM sessions WHERE id = ?", (sess_id,))
                cnt = (await cursor2.fetchone())[0] + 1
                await db.execute(
                    "UPDATE sessions SET message_count=?, last_active_at=?, updated_at=? WHERE id=?",
                    (cnt, now, now, sess_id))
                await db.execute("UPDATE messages SET session_id=? WHERE id=?", (sess_id, msg_id))
                await db.execute(
                    "INSERT INTO session_messages (session_id,message_id,role,sequence_no,created_at) VALUES (?,?,?,?,?)",
                    (sess_id, msg_id, "user", cnt, now))
                await db.commit()
            else:
                await db.execute(
                    "INSERT INTO sessions (session_key,source_platform,source_chat_id,owner_user_id,"
                    "title,topic,project,priority,status,summary_path,last_active_at,message_count,"
                    "risk_level,needs_manual_review,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f"test_{CHAT_ID[:16]}", "feishu", CHAT_ID, SENDER_ID,
                     r["topic"], r["topic"], "", "normal", "open", "", now, 1, "low", False, now, now))
                await db.commit()
                cursor2 = await db.execute("SELECT last_insert_rowid()")
                sess_id = (await cursor2.fetchone())[0]
                await db.execute("UPDATE messages SET session_id=? WHERE id=?", (sess_id, msg_id))
                await db.execute(
                    "INSERT INTO session_messages (session_id,message_id,role,sequence_no,created_at) VALUES (?,?,?,?,?)",
                    (sess_id, msg_id, "user", 1, now))
                await db.commit()

                # Create task context
                await db.execute(
                    "INSERT INTO task_contexts (title,description,status,created_at,updated_at) VALUES (?,?,?,?,?)",
                    (r["topic"], "性能优化", "active", now, now))
                await db.commit()
                cursor3 = await db.execute("SELECT last_insert_rowid()")
                tc_id = (await cursor3.fetchone())[0]
                await db.execute("UPDATE sessions SET task_context_id=? WHERE id=?", (tc_id, sess_id))
                await db.commit()

        return {
            "text": json.dumps({
                "action": "replied",
                "classified_type": "work_question",
                "topic": r["topic"],
                "project_name": None,
                "session_id": sess_id,
                "task_context_id": tc_id if idx == 0 else None,
                "reply_content": r["reply"],
                "reason": f"第{idx+1}轮处理",
            }, ensure_ascii=False),
            "session_id": f"mock-sdk-{sess_id}",
        }

    return mock_run


@pytest.fixture(autouse=True)
async def setup_test_db(tmp_path):
    """Swap DB to a temp file for test isolation."""
    global _test_db_path
    import core.database as db_mod

    db_file = tmp_path / "data" / "db" / "app.sqlite"
    db_file.parent.mkdir(parents=True)
    for d in ["memory", "reports", "audit", "sessions"]:
        (tmp_path / "data" / d).mkdir(parents=True, exist_ok=True)

    url = f"sqlite+aiosqlite:///{db_file}"
    test_engine = create_async_engine(url)
    test_factory = sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)

    orig_engine = db_mod.engine
    orig_factory = db_mod.async_session_factory
    orig_root = ac_module.PROJECT_ROOT

    db_mod.engine = test_engine
    db_mod.async_session_factory = test_factory
    ac_module.PROJECT_ROOT = tmp_path
    _test_db_path = str(db_file)

    # Also patch the imported references in modules that use `from core.database import ...`
    import core.pipeline as pipeline_mod
    orig_pipeline_factory = pipeline_mod.async_session_factory
    pipeline_mod.async_session_factory = test_factory

    async with test_engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    yield

    await test_engine.dispose()
    db_mod.engine = orig_engine
    db_mod.async_session_factory = orig_factory
    ac_module.PROJECT_ROOT = orig_root
    pipeline_mod.async_session_factory = orig_pipeline_factory


@pytest.mark.asyncio
async def test_multiturn_session_e2e():
    """3 rounds of messages → all 7 verification points pass."""
    mock_run = _make_mock_agent_run()

    with patch.object(ac_module, "agent_client") as mock_client:
        mock_client.run = AsyncMock(side_effect=mock_run)

        for i in range(3):
            msg_id = await _insert_message(i)
            await process_message(msg_id)

    # ── Verify via API ──
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:

        # 1. Messages
        resp = await c.get("/api/messages", params={"page_size": 50})
        assert resp.status_code == 200
        items = resp.json()["items"]
        user_msgs = [m for m in items if m["sender_id"] == SENDER_ID]
        bot_msgs = [m for m in items if m["classified_type"] == "bot_reply"]
        assert len(user_msgs) == 3, f"user msgs: {len(user_msgs)}"
        assert len(bot_msgs) == 3, f"bot replies: {len(bot_msgs)}"
        sids = {m["session_id"] for m in user_msgs}
        assert len(sids) == 1 and None not in sids, f"session_ids: {sids}"
        session_id = sids.pop()

        # 2. Sessions
        resp = await c.get("/api/sessions")
        assert resp.status_code == 200
        our = [s for s in resp.json()["items"] if s["source_chat_id"] == CHAT_ID]
        assert len(our) == 1
        sess = our[0]
        assert sess["id"] == session_id
        assert sess["message_count"] >= 3

        # 3. Session detail — session_messages
        resp = await c.get(f"/api/sessions/{session_id}")
        assert resp.status_code == 200
        sm = resp.json()["messages"]
        assert len([m for m in sm if m["role"] == "user"]) == 3
        assert len([m for m in sm if m["role"] == "assistant"]) == 3
        assert [m["sequence_no"] for m in sm] == sorted(m["sequence_no"] for m in sm)

        # 4. Task contexts
        resp = await c.get("/api/task-contexts")
        assert resp.status_code == 200
        tc = resp.json()["items"]
        assert len(tc) >= 1
        assert session_id in {s["id"] for s in tc[0].get("sessions", [])}

        # 5. Conversations
        resp = await c.get("/api/conversations", params={"chat_id": CHAT_ID})
        assert resp.status_code == 200
        convs = resp.json()["items"]
        assert len(convs) == 3
        for cv in convs:
            assert cv["bot_reply"] is not None
            assert cv["session_id"] == session_id

        # 6. Audit logs
        resp = await c.get("/api/audit-logs", params={"page_size": 100})
        assert resp.status_code == 200
        logs = resp.json()["items"]
        calls = [l for l in logs if l["event_type"] == "pipeline_agent_call"]
        results = [l for l in logs if l["event_type"] == "pipeline_agent_result"]
        assert len(calls) == 3, f"agent_call: {len(calls)}"
        assert len(results) == 3, f"agent_result: {len(results)}"

        for l in calls:
            d = json.loads(l["detail"])
            assert d["chat_id"] == CHAT_ID
            assert "prompt" in d

        for l in results:
            d = json.loads(l["detail"])
            assert d["action"] == "replied"

        # 7. Session context in rounds 2 & 3
        sorted_calls = sorted(calls, key=lambda l: json.loads(l["detail"])["message_id"])
        for i, l in enumerate(sorted_calls):
            d = json.loads(l["detail"])
            if i > 0:
                assert d["session_context"] is not None, f"Round {i+1}: no session_context"
                assert d["session_context"]["db_session_id"] == session_id

    print(f"\n✅ All checks passed! session_id={session_id}, "
          f"{len(user_msgs)} user + {len(bot_msgs)} bot, "
          f"{len(calls)} audit calls, {len(results)} audit results")
