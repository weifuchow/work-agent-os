"""Recover a failed reply delivery from existing analysis artifacts.

This maintenance script does not rerun the main agent or project agent. It is
for sessions where analysis completed successfully but the final Feishu reply
delivery failed, for example "feishu reply returned no result".
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any


def _find_project_root(start: Path) -> Path:
    for candidate in (start, *start.parents):
        if (candidate / "pyproject.toml").is_file() and (candidate / "core").is_dir():
            return candidate
    raise RuntimeError(f"Cannot locate work-agent-os project root from {start}")


PROJECT_ROOT = _find_project_root(Path(__file__).resolve().parent)
sys.path.insert(0, str(PROJECT_ROOT))

from core.app.context import MessageContext, PreparedWorkspace
from core.app.result_handler import ResultHandler
from core.connectors.feishu import FeishuChannelPort
from core.deps import SystemClock
from core.ports import ReplyPayload
from core.repositories import Repository


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _fetch_row(db_path: Path, sql: str, params: tuple[Any, ...]) -> dict[str, Any]:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(sql, params).fetchone()
        return dict(row) if row else {}
    finally:
        con.close()


def _session_dir(repo: Path, session_id: int) -> Path:
    return repo / "data" / "sessions" / f"session-{session_id}"


def _artifact_roots(session_dir: Path) -> dict[str, str]:
    workspace = _load_json(session_dir / "session_workspace.json")
    roots = workspace.get("session_artifact_roots")
    return {str(k): str(v) for k, v in roots.items()} if isinstance(roots, dict) else {}


def _latest_dispatch_file(session_dir: Path, message_id: int) -> Path | None:
    dispatch_dir = session_dir / ".orchestration" / f"message-{message_id}"
    if not dispatch_dir.exists():
        return None
    candidates = sorted(dispatch_dir.glob("dispatch-*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def _summary_to_markdown(summary: dict[str, Any]) -> str:
    parts: list[str] = []
    title = str(summary.get("title") or "分析结果").strip()
    if title:
        parts.append(f"**{title}**")
    lead = str(summary.get("summary") or summary.get("fallback_text") or "").strip()
    if lead:
        parts.append(lead)
    sections = summary.get("sections")
    if isinstance(sections, list):
        for section in sections[:6]:
            if not isinstance(section, dict):
                continue
            section_title = str(section.get("title") or "").strip()
            content = str(section.get("content") or "").strip()
            if section_title and content:
                parts.append(f"**{section_title}**\n{content}")
            elif content:
                parts.append(content)
    confidence = str(summary.get("confidence") or "").strip()
    if confidence:
        parts.append(f"置信度：{confidence}")
    project_used = summary.get("project_used")
    if isinstance(project_used, dict):
        context = []
        for label, key in (
            ("项目", "project"),
            ("版本", "worktree_version"),
            ("检出", "checkout_ref"),
            ("提交", "worktree_commit"),
        ):
            value = str(project_used.get(key) or "").strip()
            if value:
                context.append(f"- {label}：{value}")
        if context:
            parts.append("**运行上下文**\n" + "\n".join(context))
    return "\n\n".join(parts).strip()


def _reply_from_artifacts(session_dir: Path, message_id: int) -> ReplyPayload:
    dispatch_file = _latest_dispatch_file(session_dir, message_id)
    dispatch = _load_json(dispatch_file) if dispatch_file else {}
    analysis_dir = Path(str(dispatch.get("analysis_dir") or ""))
    summary = _load_json(analysis_dir / "artifacts" / "summary.json") if analysis_dir else {}
    if not summary:
        summary = _load_json(analysis_dir / "final_summary.json") if analysis_dir else {}
    content = _summary_to_markdown(summary) if summary else ""
    if not content:
        result = _load_json(analysis_dir / "result.json") if analysis_dir else {}
        raw = result.get("raw") if isinstance(result.get("raw"), dict) else {}
        content = str(result.get("output_text") or raw.get("text") or "").strip()
    if not content:
        raise RuntimeError(f"No recoverable reply content found for message {message_id}")
    return ReplyPayload(
        channel="feishu",
        type="markdown",
        content=content,
        metadata={
            "delivery_recovered_from": str(dispatch_file or ""),
            "delivery_recovery_reason": "previous reply_delivery failed",
        },
    )


async def recover(repo: Path, session_id: int, message_id: int) -> None:
    db_path = repo / "data" / "db" / "app.sqlite"
    session_dir = _session_dir(repo, session_id)
    message = _fetch_row(db_path, "SELECT * FROM messages WHERE id = ?", (message_id,))
    if not message:
        raise RuntimeError(f"message not found: {message_id}")
    session = _fetch_row(db_path, "SELECT * FROM sessions WHERE id = ?", (session_id,))
    if not session:
        raise RuntimeError(f"session not found: {session_id}")

    workspace_dir = session_dir / "workspace"
    workspace = PreparedWorkspace(
        path=workspace_dir,
        input_dir=workspace_dir / "input",
        state_dir=workspace_dir / "state",
        output_dir=workspace_dir / "output",
        artifacts_dir=workspace_dir / "artifacts",
        artifact_roots=_artifact_roots(session_dir),
        media_manifest=_load_json(workspace_dir / "input" / "media_manifest.json"),
        skill_registry=_load_json(workspace_dir / "input" / "skill_registry.json"),
    )
    ctx = MessageContext(message=message, session=session, history=[])
    reply = _reply_from_artifacts(session_dir, message_id)
    handler = ResultHandler(
        repository=Repository(db_path),
        channel_port=FeishuChannelPort(),
        clock=SystemClock(),
        reply_repairer=None,
    )
    await handler._deliver_reply(ctx, workspace, reply)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=str(PROJECT_ROOT))
    parser.add_argument("--session", type=int, required=True)
    parser.add_argument("--message", type=int, required=True)
    args = parser.parse_args()
    asyncio.run(recover(Path(args.repo).resolve(), args.session, args.message))
    print(f"Recovered reply delivery for session-{args.session} message-{args.message}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
