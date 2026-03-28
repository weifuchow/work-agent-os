"""批量分类未分类消息。

用法: python -m skills.intake.scripts.classify
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

import models.db  # noqa: F401, E402
from core.orchestrator.agent_client import agent_client  # noqa: E402


async def main():
    import aiosqlite
    db_path = Path("data/db/app.sqlite")
    async with aiosqlite.connect(str(db_path)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, content, sender_id, chat_id FROM messages WHERE classified_type IS NULL ORDER BY created_at DESC LIMIT 10"
        )
        rows = await cursor.fetchall()

    if not rows:
        print("No unclassified messages found.")
        return

    for row in rows:
        msg_id, content, sender, chat = row
        print(f"\n--- Message #{msg_id} from {sender} ---")
        print(f"Content: {content[:100]}")

        result = await agent_client.run(
            prompt=f"分类这条消息：\n发送者: {sender}\n聊天: {chat}\n内容: {content}",
            skill="intake",
            max_turns=3,
        )
        print(f"Result: {result['text'][:300]}")
        print(f"Session: {result['session_id']}")


if __name__ == "__main__":
    asyncio.run(main())
