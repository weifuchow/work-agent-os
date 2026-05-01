"""Workspace preparation for agent/skill execution."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.app.context import MessageContext, PreparedWorkspace
from core.artifacts.manifest import discover_skill_registry, write_json
from core.artifacts.media import MediaStager
from core.config import settings
from core.ports import FilePort


class WorkspacePreparer:
    def __init__(self, file_port: FilePort, workspace_root: Path | None = None) -> None:
        self.media = MediaStager(file_port)
        self.workspace_root = workspace_root

    async def prepare(self, ctx: MessageContext) -> PreparedWorkspace:
        workspace = self._workspace_path(ctx)
        artifact_roots = _artifact_roots(workspace, ctx.session_id)
        input_dir = workspace / "input"
        state_dir = workspace / "state"
        output_dir = workspace / "output"
        artifacts_dir = workspace / "artifacts"
        for path in (
            input_dir,
            state_dir,
            output_dir,
            artifacts_dir,
            *[Path(value) for value in artifact_roots.values()],
        ):
            path.mkdir(parents=True, exist_ok=True)

        media_manifest = await self.media.stage(
            ctx.message,
            artifacts_dir,
            uploads_dir=Path(artifact_roots["uploads_dir"]),
        )
        skill_registry = discover_skill_registry()

        write_json(input_dir / "message.json", _message_payload(ctx.message))
        write_json(input_dir / "session.json", _session_payload(ctx.session))
        write_json(input_dir / "history.json", [_history_payload(item) for item in ctx.history])
        write_json(input_dir / "media_manifest.json", media_manifest)
        write_json(input_dir / "artifact_roots.json", artifact_roots)
        session_workspace_path = Path(artifact_roots["session_dir"]) / "session_workspace.json"
        write_json(
            session_workspace_path,
            _session_workspace_map(
                artifact_roots=artifact_roots,
            ),
        )
        write_json(
            input_dir / "project_context.json",
            {
                "artifact_roots": artifact_roots,
                "session_workspace_path": str(session_workspace_path.resolve()),
            },
        )
        write_json(input_dir / "skill_registry.json", skill_registry)
        write_json(state_dir / "state.json", {})

        return PreparedWorkspace(
            path=workspace,
            input_dir=input_dir,
            state_dir=state_dir,
            output_dir=output_dir,
            artifacts_dir=artifacts_dir,
            artifact_roots=artifact_roots,
            media_manifest=media_manifest,
            skill_registry=skill_registry,
        )

    def _workspace_path(self, ctx: MessageContext) -> Path:
        base = self.workspace_root or settings.sessions_dir
        session_part = f"session-{ctx.session_id}" if ctx.session_id else "no-session"
        return base / session_part / "workspace"


def _artifact_roots(workspace: Path, session_id: int | None) -> dict[str, str]:
    session_dir = workspace.parent
    return {
        "session_dir": str(session_dir.resolve()),
        "workspace_dir": str(workspace.resolve()),
        "ones_dir": str((session_dir / ".ones").resolve()),
        "triage_dir": str((session_dir / ".triage").resolve()),
        "review_dir": str((session_dir / ".review").resolve()),
        "worktrees_dir": str((session_dir / "worktrees").resolve()),
        "uploads_dir": str((session_dir / "uploads").resolve()),
        "attachments_dir": str((session_dir / "attachments").resolve()),
        "scratch_dir": str((session_dir / "scratch").resolve()),
    }


def _session_workspace_map(
    *,
    artifact_roots: dict[str, str],
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "session_dir": artifact_roots["session_dir"],
        "workspace_dir": artifact_roots["workspace_dir"],
        "session_artifact_roots": artifact_roots,
        "lookup_order": [
            "workspace/input/message.json",
            "workspace/input/session.json",
            "workspace/input/history.json",
            "workspace/input/media_manifest.json",
            "workspace/input/skill_registry.json",
            "session_artifact_roots.uploads_dir",
            "session_artifact_roots.ones_dir",
            "session_artifact_roots.triage_dir",
            "session_artifact_roots.review_dir",
            "session_artifact_roots.worktrees_dir",
        ],
        "write_policy": {
            "final_reply_contract": "workspace/output",
            "structured_summary": "workspace/output",
            "runtime_state": "workspace/state",
            "raw_user_uploads": "session_artifact_roots.uploads_dir",
            "extracted_or_curated_attachments": "session_artifact_roots.attachments_dir",
            "ones_artifacts": "session_artifact_roots.ones_dir",
            "triage_artifacts": "session_artifact_roots.triage_dir/<topic>",
            "triage_intake": "session_artifact_roots.triage_dir/<topic>/01-intake",
            "triage_process": "session_artifact_roots.triage_dir/<topic>/02-process",
            "triage_search_runs": "session_artifact_roots.triage_dir/<topic>/search-runs",
            "triage_state_and_dsl": "session_artifact_roots.triage_dir/<topic>/00-state.json + keyword_package.roundN.json + query.roundN.dsl.txt",
            "review_artifacts": "session_artifact_roots.review_dir",
            "project_worktrees": "session_artifact_roots.worktrees_dir/<project>/<task-version>",
            "scratch_files": "session_artifact_roots.scratch_dir",
        },
        "forbidden_roots": [
            ".ones",
            ".triage",
            ".review",
            ".session",
            "data/attachments",
        ],
    }


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
