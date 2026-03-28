"""根据 session_id 构建上下文包。

用法: python -m skills.context.scripts.build_context --session-id 1
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

import models.db  # noqa: F401, E402
from core.orchestrator.agent_client import agent_client  # noqa: E402


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-id", type=int, required=True)
    args = parser.parse_args()

    result = await agent_client.run(
        prompt=f"为 session_id={args.session_id} 构建上下文包。先用 query_db 查询该 session 的最近消息，再检索相关记忆文件。",
        skill="context",
        max_turns=5,
    )
    print(result["text"])


if __name__ == "__main__":
    asyncio.run(main())
