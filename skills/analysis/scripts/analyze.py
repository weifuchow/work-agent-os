"""对指定 session 运行问题分析。

用法: python -m skills.analysis.scripts.analyze --session-id 1
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
        prompt=f"对 session_id={args.session_id} 的工作问题进行结构化分析。先查询会话消息，再输出完整分析报告。",
        skill="analysis",
        max_turns=5,
    )
    print(result["text"])


if __name__ == "__main__":
    asyncio.run(main())
