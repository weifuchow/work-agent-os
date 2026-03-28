"""Feishu webhook & send message router."""

from fastapi import APIRouter
from pydantic import BaseModel

from core.connectors.feishu import FeishuClient

router = APIRouter(prefix="/feishu", tags=["feishu"])


class SendMessageRequest(BaseModel):
    receive_id: str
    content: str
    receive_id_type: str = "chat_id"
    msg_type: str = "text"


class ReplyMessageRequest(BaseModel):
    message_id: str
    content: str
    msg_type: str = "text"


@router.post("/webhook")
async def feishu_webhook(body: dict = {}):
    """Backup webhook endpoint (primary is WebSocket long-connection)."""
    # Handle URL verification challenge
    if "challenge" in body:
        return {"challenge": body["challenge"]}
    return {"status": "ok"}


@router.post("/send")
async def send_message(req: SendMessageRequest):
    """Send a message via Feishu (used by admin UI)."""
    client = FeishuClient()
    ok = client.send_message(
        receive_id=req.receive_id,
        content=req.content,
        receive_id_type=req.receive_id_type,
        msg_type=req.msg_type,
    )
    return {"success": ok}


@router.post("/reply")
async def reply_message(req: ReplyMessageRequest):
    """Reply to a specific Feishu message."""
    client = FeishuClient()
    ok = client.reply_message(
        message_id=req.message_id,
        content=req.content,
        msg_type=req.msg_type,
    )
    return {"success": ok}
