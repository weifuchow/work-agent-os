"""手动触发日报生成。

用法: python .claude/skills/daily-report/scripts/generate_daily.py [--date 2026-03-27]
"""

import argparse
import asyncio
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

import models.db  # noqa: F401, E402
from core.orchestrator.agent_client import agent_client  # noqa: E402


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=None, help="日报日期 YYYY-MM-DD，默认昨天")
    args = parser.parse_args()

    report_date = args.date or str(date.today() - timedelta(days=1))

    result = await agent_client.run(
        prompt=f"生成 {report_date} 的工作日报。查询该日所有工作会话，汇总后保存到 data/reports/daily/{report_date}.md",
        skill="daily-report",
        max_turns=8,
    )
    print(result["text"])
    print(f"\nSession: {result['session_id']}")


if __name__ == "__main__":
    asyncio.run(main())
