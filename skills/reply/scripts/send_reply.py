"""发送已确认的草稿回复。

用法: python -m skills.reply.scripts.send_reply --chat-id oc_xxx --content "回复内容"
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

from core.connectors.feishu import FeishuClient  # noqa: E402


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--chat-id", required=True)
    parser.add_argument("--content", required=True)
    args = parser.parse_args()

    client = FeishuClient()
    ok = client.send_message(receive_id=args.chat_id, content=args.content)
    print("Sent!" if ok else "Failed!")


if __name__ == "__main__":
    asyncio.run(main())
