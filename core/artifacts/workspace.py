"""Workspace preparation for agent/skill execution."""

from __future__ import annotations

from pathlib import Path

from core.app.context import MessageContext, PreparedWorkspace
from core.artifacts.manifest import discover_skill_registry, write_json
from core.artifacts.media import MediaStager
from core.artifacts.session_init import initialize_session_workspace
from core.config import settings
from core.ports import FilePort


class WorkspacePreparer:
    def __init__(self, file_port: FilePort, workspace_root: Path | None = None) -> None:
        self.media = MediaStager(file_port)
        self.workspace_root = workspace_root

    async def prepare(self, ctx: MessageContext) -> PreparedWorkspace:
        workspace = self._workspace_path(ctx)
        initialized = initialize_session_workspace(workspace, ctx.session_id)

        media_manifest = await self.media.stage(
            ctx.message,
            initialized.artifacts_dir,
            uploads_dir=Path(initialized.artifact_roots["uploads_dir"]),
        )
        skill_registry = discover_skill_registry()

        write_json(initialized.input_dir / "message.json", _message_payload(ctx.message))
        write_json(initialized.input_dir / "session.json", _session_payload(ctx.session))
        write_json(initialized.input_dir / "history.json", [_history_payload(item) for item in ctx.history])
        write_json(initialized.input_dir / "media_manifest.json", media_manifest)
        write_json(initialized.input_dir / "artifact_roots.json", initialized.artifact_roots)
        write_json(initialized.input_dir / "skill_registry.json", skill_registry)
        write_json(initialized.state_dir / "state.json", {})

        return PreparedWorkspace(
            path=initialized.workspace,
            input_dir=initialized.input_dir,
            state_dir=initialized.state_dir,
            output_dir=initialized.output_dir,
            artifacts_dir=initialized.artifacts_dir,
            artifact_roots=initialized.artifact_roots,
            media_manifest=media_manifest,
            skill_registry=skill_registry,
        )

    def _workspace_path(self, ctx: MessageContext) -> Path:
        base = self.workspace_root or settings.sessions_dir
        session_part = f"session-{ctx.session_id}" if ctx.session_id else "no-session"
        return base / session_part / "workspace"

def _message_payload(msg: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": msg.get("id"),
        "platform": msg.get("platform") or "feishu",
        "platform_message_id": msg.get("platform_message_id") or "",
        "chat_id": msg.get("chat_id") or "",
        "thread_id": msg.get("thread_id") or "",
        "sender_id": msg.get("sender_id") or "",
        "sender_name": msg.get("sender_name") or "",
        "message_type": msg.get("message_type") or "text",
        "content": msg.get("content") or "",
        "received_at": _stringify_time(msg.get("received_at") or msg.get("created_at") or ""),
        "raw_event_path": "",
    }


def _session_payload(session: dict[str, Any] | None) -> dict[str, Any]:
    if not session:
        return {}
    return {
        "id": session.get("id"),
        "session_key": session.get("session_key") or "",
        "source_platform": session.get("source_platform") or "",
        "source_chat_id": session.get("source_chat_id") or "",
        "owner_user_id": session.get("owner_user_id") or "",
        "title": session.get("title") or "",
        "topic": session.get("topic") or "",
        "project": session.get("project") or "",
        "status": session.get("status") or "",
        "thread_id": session.get("thread_id") or "",
        "agent_session_id": session.get("agent_session_id") or "",
        "agent_runtime": session.get("agent_runtime") or "",
        "summary_path": session.get("summary_path") or "",
        "last_active_at": _stringify_time(session.get("last_active_at") or ""),
    }


def _history_payload(msg: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": msg.get("id"),
        "role": "assistant" if msg.get("sender_id") == "bot" else "user",
        "message_type": msg.get("message_type") or "text",
        "content": msg.get("content") or "",
        "created_at": _stringify_time(msg.get("created_at") or msg.get("received_at") or ""),
    }


def _stringify_time(value: Any) -> str:
    if value is None:
        return ""
    return value.isoformat() if hasattr(value, "isoformat") else str(value)
