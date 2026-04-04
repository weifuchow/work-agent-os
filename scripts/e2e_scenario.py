"""End-to-end scenario runner.

Runs two multi-turn conversations through the real pipeline (real Claude API):

  Scenario A — Project (allspark):
    1. allspark 是什么
    2. 简单项目支持的数据源
    3. 这个项目 sqlserver 怎么配置

  Scenario B — Non-project (music):
    1. radiohead 和 queen 乐队对比
    2. radiohead 什么歌好听
    3. 分析一下他们为什么抓人

Usage:
    python scripts/e2e_scenario.py            # run both scenarios
    python scripts/e2e_scenario.py --scenario a   # project only
    python scripts/e2e_scenario.py --scenario b   # music only
    python scripts/e2e_scenario.py --dry-run      # show plan, skip API calls
"""

import argparse
import asyncio
import json
import sys
import uuid
from datetime import datetime
from pathlib import Path

# Make project root importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiosqlite
from loguru import logger

logger.remove()
logger.add(sys.stderr, level="WARNING")  # suppress noisy INFO logs during scenario run

DB_PATH = str(Path(__file__).resolve().parent.parent / "data" / "db" / "app.sqlite")
CHAT_ID = "oc_e2e_scenario_runner"
SENDER_ID = "ou_e2e_tester"
SENDER_NAME = "E2E Runner"


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

async def insert_message(content: str, thread_id: str = "") -> int:
    now = datetime.now().isoformat()
    mid = f"e2e_{uuid.uuid4().hex[:10]}"
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO messages (platform, platform_message_id, chat_id, sender_id, "
            "sender_name, message_type, content, received_at, thread_id, "
            "pipeline_status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("feishu", mid, CHAT_ID, SENDER_ID, SENDER_NAME, "text",
             content, now, thread_id, "pending", now),
        )
        await db.commit()
        return cursor.lastrowid


async def get_message(msg_id: int) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM messages WHERE id = ?", (msg_id,))
        row = await cursor.fetchone()
        return dict(row) if row else {}


async def get_session(session_id: int) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,))
        row = await cursor.fetchone()
        return dict(row) if row else {}


async def get_bot_reply(platform_message_id: str) -> str:
    """Get the bot reply content for a given original message."""
    reply_mid = f"reply_{platform_message_id}"
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT content FROM messages WHERE platform_message_id = ?",
            (reply_mid,),
        )
        row = await cursor.fetchone()
        return row[0] if row else ""


# ---------------------------------------------------------------------------
# Turn runner
# ---------------------------------------------------------------------------

async def run_turn(
    turn_no: int,
    content: str,
    thread_id: str = "",
    dry_run: bool = False,
) -> dict:
    """Run one conversation turn. Returns state dict with session/reply info."""
    print(f"\n  Turn {turn_no}: {content!r}")

    if dry_run:
        print(f"  [DRY RUN] would call process_message with thread_id={thread_id!r}")
        return {"thread_id": thread_id or "dry-run-thread", "session_id": 0,
                "agent_session_id": "dry-run-sid", "project": ""}

    msg_id = await insert_message(content, thread_id=thread_id)

    from core.pipeline import process_message
    await process_message(msg_id)

    msg = await get_message(msg_id)
    status = msg.get("pipeline_status", "?")
    session_id = msg.get("session_id")

    if status != "completed":
        print(f"  ❌ status={status}  error={msg.get('pipeline_error', '')[:100]}")
        return {"error": True, "status": status}

    session = await get_session(session_id) if session_id else {}
    reply = await get_bot_reply(msg["platform_message_id"])

    # Truncate long replies for display
    reply_display = reply[:300] + ("..." if len(reply) > 300 else "")

    print(f"  ✓ status={status}  classified={msg.get('classified_type', '?')}")
    print(f"  session_id={session_id}  project={session.get('project', '')!r}")
    print(f"  agent_session_id={session.get('agent_session_id', '')!r}")
    print(f"  thread_id={session.get('thread_id', '')!r}")
    print(f"  reply: {reply_display}")

    return {
        "msg_id": msg_id,
        "session_id": session_id,
        "thread_id": session.get("thread_id", ""),
        "agent_session_id": session.get("agent_session_id", ""),
        "project": session.get("project", ""),
        "classified_type": msg.get("classified_type", ""),
        "reply": reply,
    }


# ---------------------------------------------------------------------------
# Scenario A: Project (allspark)
# ---------------------------------------------------------------------------

async def run_scenario_a(dry_run: bool = False):
    print("\n" + "=" * 60)
    print("Scenario A — Project (allspark)")
    print("=" * 60)

    turns = [
        "allspark 是什么",
        "简单项目支持的数据源",
        "这个项目 sqlserver 怎么配置",
    ]

    thread_id = ""
    session_id = None
    agent_session_ids = []

    for i, content in enumerate(turns, 1):
        state = await run_turn(i, content, thread_id=thread_id, dry_run=dry_run)
        if state.get("error"):
            print(f"\n  Scenario A stopped at turn {i} due to error.")
            return

        # Use thread_id from first reply for subsequent turns
        if not thread_id and state.get("thread_id"):
            thread_id = state["thread_id"]

        if session_id is None:
            session_id = state.get("session_id")

        agent_session_ids.append(state.get("agent_session_id", ""))

    # --- Summary ---
    print(f"\n  {'─' * 40}")
    print("  Scenario A Summary:")
    print(f"  session_id     : {session_id}")
    print(f"  thread_id      : {thread_id!r}")
    print(f"  project        : {state.get('project', '')!r}")

    # Verify session consistency
    all_same_session = len({s for s in [session_id] if s}) == 1
    agent_sids_unique = list(dict.fromkeys(agent_session_ids))  # deduplicated, order preserved

    print(f"  agent_session_ids across turns:")
    for i, sid in enumerate(agent_session_ids, 1):
        marker = "← same (resume)" if i > 1 and sid == agent_session_ids[0] else ""
        print(f"    Turn {i}: {sid!r} {marker}")

    # Continuity checks
    if not dry_run:
        ok = True
        if state.get("project") != "allspark":
            print(f"\n  ⚠️  WARN: expected project='allspark', got {state.get('project')!r}")
            ok = False
        if not agent_session_ids[0]:
            print(f"\n  ⚠️  WARN: agent_session_id not set after turn 1")
            ok = False
        # Turn 2 and 3 should use same agent_session_id (resume)
        if len(agent_session_ids) >= 2 and agent_session_ids[1] != agent_session_ids[0]:
            print(f"\n  ⚠️  WARN: agent_session_id changed between turn 1 and 2 "
                  f"(expected same for resume)")
            ok = False
        if ok:
            print(f"\n  ✅ Scenario A: continuity OK — all turns in same session, resume working")


# ---------------------------------------------------------------------------
# Scenario B: Non-project (music)
# ---------------------------------------------------------------------------

async def run_scenario_b(dry_run: bool = False):
    print("\n" + "=" * 60)
    print("Scenario B — Non-project (music)")
    print("=" * 60)

    turns = [
        "radiohead 和 queen 乐队对比",
        "radiohead 什么歌好听",
        "分析一下他们为什么抓人",
    ]

    thread_id = ""
    session_id = None
    projects = []
    agent_session_ids = []

    for i, content in enumerate(turns, 1):
        state = await run_turn(i, content, thread_id=thread_id, dry_run=dry_run)
        if state.get("error"):
            print(f"\n  Scenario B stopped at turn {i} due to error.")
            return

        if not thread_id and state.get("thread_id"):
            thread_id = state["thread_id"]

        if session_id is None:
            session_id = state.get("session_id")

        projects.append(state.get("project", ""))
        agent_session_ids.append(state.get("agent_session_id", ""))

    # --- Summary ---
    print(f"\n  {'─' * 40}")
    print("  Scenario B Summary:")
    print(f"  session_id      : {session_id}")
    print(f"  thread_id       : {thread_id!r}")
    print(f"  projects across turns: {projects}")
    print(f"  agent_session_ids: {agent_session_ids}")

    if not dry_run:
        ok = True
        for i, p in enumerate(projects, 1):
            if p:
                print(f"\n  ⚠️  WARN: Turn {i} unexpectedly got project={p!r} (should be empty)")
                ok = False
        if ok:
            print(f"\n  ✅ Scenario B: no project binding, all turns through orchestrator")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    parser = argparse.ArgumentParser(description="E2E scenario runner")
    parser.add_argument("--scenario", choices=["a", "b"], help="Run only one scenario")
    parser.add_argument("--dry-run", action="store_true", help="Print plan without calling API")
    args = parser.parse_args()

    print(f"E2E Scenario Runner  [{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]")
    print(f"DB: {DB_PATH}")
    if args.dry_run:
        print("MODE: DRY RUN (no API calls)")

    if args.scenario == "a":
        await run_scenario_a(dry_run=args.dry_run)
    elif args.scenario == "b":
        await run_scenario_b(dry_run=args.dry_run)
    else:
        await run_scenario_a(dry_run=args.dry_run)
        await run_scenario_b(dry_run=args.dry_run)

    print("\n" + "=" * 60)
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
