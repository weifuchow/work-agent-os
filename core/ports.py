"""Core dependency ports.

Ports describe platform capabilities without embedding product or business
workflow decisions in the message processor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class ReplyPayload:
    """A channel-agnostic reply produced by the agent/skill layer."""

    channel: str = "feishu"
    type: str = "text"
    content: str = ""
    payload: Any = None
    intent: str | None = None
    file_path: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ReplyPayload":
        metadata = {
            key: item
            for key, item in value.items()
            if key not in {"channel", "type", "content", "payload", "intent", "file_path"}
        }
        return cls(
            channel=str(value.get("channel") or "feishu"),
            type=str(value.get("type") or "text"),
            content=str(value.get("content") or ""),
            payload=value.get("payload"),
            intent=str(value.get("intent") or "").strip() or None,
            file_path=str(value.get("file_path") or "").strip() or None,
            metadata=metadata,
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "channel": self.channel,
            "type": self.type,
            "content": self.content,
            "payload": self.payload,
        }
        if self.intent:
            result["intent"] = self.intent
        if self.file_path:
            result["file_path"] = self.file_path
        result.update(self.metadata)
        return result


@dataclass(frozen=True)
class DeliveryResult:
    delivered: bool
    message_id: str = ""
    thread_id: str = ""
    root_id: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    error: str = ""


@dataclass(frozen=True)
class AgentRequest:
    workspace_path: Path
    message: dict[str, Any]
    session: dict[str, Any] | None
    history: list[dict[str, Any]]
    skill_registry: dict[str, Any]
    mode: str = "process"
    repair_context: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentResponse:
    text: str = ""
    session_id: str | None = None
    runtime: str = ""
    model: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DownloadedFile:
    data: bytes
    file_name: str = ""
    mime_type: str = ""


class ChannelPort(Protocol):
    async def deliver_reply(
        self,
        *,
        source_message: dict[str, Any],
        reply: ReplyPayload,
    ) -> DeliveryResult:
        """Deliver a prepared payload to a channel adapter."""


class AgentPort(Protocol):
    async def run(self, request: AgentRequest) -> AgentResponse:
        """Run the agent runtime against a prepared workspace."""


class ReplyRepairPort(Protocol):
    async def repair_reply(
        self,
        *,
        ctx: Any,
        workspace: Any,
        reply: ReplyPayload,
        validation_errors: list[dict[str, Any]],
    ) -> ReplyPayload | None:
        """Ask the agent runtime to repair an invalid reply payload."""


class FilePort(Protocol):
    async def download_message_media(
        self,
        *,
        source_message: dict[str, Any],
        media_info: dict[str, Any],
    ) -> DownloadedFile | None:
        """Download a platform media resource, if the platform supports it."""


class ClockPort(Protocol):
    def now_iso(self) -> str:
        """Current local timestamp as an ISO string."""
