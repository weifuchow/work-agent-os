from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.ports import AgentRequest, AgentResponse, DeliveryResult, DownloadedFile, ReplyPayload


class FixedClock:
    def __init__(self, value: str = "2026-04-30T10:00:00") -> None:
        self.value = value

    def now_iso(self) -> str:
        return self.value


class FakeAgentPort:
    def __init__(self, *results: dict[str, Any] | AgentResponse) -> None:
        self.results = list(results)
        self.calls: list[AgentRequest] = []

    async def run(self, request: AgentRequest) -> AgentResponse:
        self.calls.append(request)
        result = self.results.pop(0)
        if isinstance(result, AgentResponse):
            return result
        return AgentResponse(
            text=json.dumps(result, ensure_ascii=False),
            session_id=str(result.get("agent_session_id") or "agent-session-1"),
            runtime="fake",
            usage={"input_tokens": 11, "output_tokens": 7},
            raw={"cost_usd": 0.0},
        )


class FakeChannelPort:
    def __init__(self, *, delivered: bool = True, thread_id: str = "omt_reply_001") -> None:
        self.delivered = delivered
        self.thread_id = thread_id
        self.calls: list[dict[str, Any]] = []

    async def deliver_reply(
        self,
        *,
        source_message: dict[str, Any],
        reply: ReplyPayload,
    ) -> DeliveryResult:
        self.calls.append({"source_message": source_message, "reply": reply})
        if not self.delivered:
            return DeliveryResult(delivered=False, error="fake delivery failed")
        return DeliveryResult(
            delivered=True,
            message_id=f"reply_{source_message['platform_message_id']}",
            thread_id=self.thread_id,
            root_id="om_root_001",
        )


class FakeFilePort:
    def __init__(self, files: dict[str, DownloadedFile] | None = None) -> None:
        self.files = files or {}
        self.calls: list[dict[str, Any]] = []

    async def download_message_media(
        self,
        *,
        source_message: dict[str, Any],
        media_info: dict[str, Any],
    ) -> DownloadedFile | None:
        self.calls.append({"source_message": source_message, "media_info": media_info})
        key = str(
            media_info.get("resource_id")
            or media_info.get("image_key")
            or media_info.get("file_key")
            or ""
        )
        return self.files.get(key)


def read_workspace_json(workspace: Path, relative_path: str) -> dict[str, Any]:
    return json.loads((workspace / relative_path).read_text(encoding="utf-8"))
