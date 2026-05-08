from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
from typing import Any


@dataclass
class ReplayPaths:
    replay_root: Path
    db_path: Path
    sessions_root: Path
    session_dir: Path


@dataclass
class MockDelivery:
    source_message_id: int
    reply_type: str
    intent: str
    content_preview: str
    payload_preview: str


class MockFeishuChannel:
    def __init__(self) -> None:
        self.deliveries: list[MockDelivery] = []

    async def deliver_reply(
        self,
        *,
        source_message: dict[str, Any],
        reply: Any,
    ) -> Any:
        from core.ports import DeliveryResult

        payload_text = ""
        if getattr(reply, "payload", None) is not None:
            payload_text = json.dumps(reply.payload, ensure_ascii=False)[:2000]
        self.deliveries.append(
            MockDelivery(
                source_message_id=int(source_message.get("id") or 0),
                reply_type=str(getattr(reply, "type", "")),
                intent=str(getattr(reply, "intent", "") or ""),
                content_preview=str(getattr(reply, "content", "") or "")[:2000],
                payload_preview=payload_text,
            )
        )
        thread_id = str(source_message.get("thread_id") or "")
        root_id = str(source_message.get("root_id") or "")
        return DeliveryResult(
            delivered=True,
            message_id=f"mock_feishu_reply_{source_message.get('id')}",
            thread_id=thread_id or f"mock_thread_session_{source_message.get('session_id') or ''}",
            root_id=root_id or f"mock_root_session_{source_message.get('session_id') or ''}",
            raw={"mock": True, "replay": True},
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Replay a work-agent-os session message through the real pipeline with mocked Feishu delivery."
    )
    parser.add_argument("--repo", default=".", help="work-agent-os repository root")
    parser.add_argument("--session", required=True, help="Session id, e.g. 175 or session-175")
    parser.add_argument("--message", type=int, help="Message id to replay. Defaults to the latest user message.")
    parser.add_argument(
        "--output-root",
        default="",
        help="Replay output root. Defaults to <repo>/.tmp/session-replays.",
    )
    parser.add_argument("--label", default="", help="Optional label appended to session-replay directory name.")
    parser.add_argument("--no-run", action="store_true", help="Only create replay data, do not run the pipeline.")
    parser.add_argument(
        "--keep-assistant-after",
        action="store_true",
        help="Keep assistant replies after the replayed message in the DB copy.",
    )
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))

    session_id = _parse_session_id(args.session)
    source_db = repo / "data" / "db" / "app.sqlite"
    if not source_db.exists():
        raise SystemExit(f"DB not found: {source_db}")

    message_id = int(args.message or _latest_user_message_id(source_db, session_id))
    paths = _create_replay_paths(repo, session_id, args.output_root, args.label)
    _copy_replay_db(source_db, paths.db_path, session_id, message_id, keep_assistant_after=args.keep_assistant_after)
    _copy_session_files(repo / "data" / "sessions" / f"session-{session_id}", paths.session_dir)

    result: dict[str, Any] = {
        "repo": str(repo),
        "session_id": session_id,
        "message_id": message_id,
        "replay_id": paths.replay_root.name,
        "replay_root": str(paths.replay_root),
        "db_path": str(paths.db_path),
        "sessions_root": str(paths.sessions_root),
        "session_dir": str(paths.session_dir),
        "mode": "prepared" if args.no_run else "pipeline",
    }
    if not args.no_run:
        run_result = asyncio.run(_run_pipeline(repo, paths, session_id, message_id))
        result.update(run_result)

    summary_path = paths.replay_root / "replay_summary.json"
    summary_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    result["summary_path"] = str(summary_path)

    print(json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0


def _parse_session_id(value: str) -> int:
    text = str(value).strip().replace("\\", "/").rstrip("/")
    name = text.split("/")[-1]
    if name.startswith("session-"):
        name = name[len("session-"):]
    return int(name)


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _rows(db_path: Path, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with _connect(db_path) as conn:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]


def _one(db_path: Path, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any]:
    rows = _rows(db_path, sql, params)
    return rows[0] if rows else {}


def _latest_user_message_id(db_path: Path, session_id: int) -> int:
    row = _one(
        db_path,
        """
        SELECT m.id
        FROM session_messages sm
        JOIN messages m ON m.id = sm.message_id
        WHERE sm.session_id = ?
          AND COALESCE(sm.role, '') != 'assistant'
          AND COALESCE(m.sender_id, '') != 'bot'
          AND COALESCE(m.classified_type, '') != 'bot_reply'
        ORDER BY sm.sequence_no DESC, sm.id DESC
        LIMIT 1
        """,
        (session_id,),
    )
    if not row:
        raise SystemExit(f"No user message found for session {session_id}")
    return int(row["id"])


def _create_replay_paths(repo: Path, session_id: int, output_root: str, label: str) -> ReplayPaths:
    root = Path(output_root).resolve() if output_root else repo / ".tmp" / "session-replays"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_label = _safe_token(label)
    suffix = f"-{safe_label}" if safe_label else ""
    replay_root = root / f"session-replay-{session_id}-{stamp}{suffix}"
    replay_root.mkdir(parents=True, exist_ok=False)
    sessions_root = replay_root / "sessions"
    session_dir = sessions_root / f"session-{session_id}"
    session_dir.mkdir(parents=True, exist_ok=True)
    return ReplayPaths(
        replay_root=replay_root,
        db_path=replay_root / "app.sqlite",
        sessions_root=sessions_root,
        session_dir=session_dir,
    )


def _safe_token(value: str) -> str:
    text = str(value or "").strip()
    return "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in text).strip("-")


def _copy_replay_db(
    source_db: Path,
    replay_db: Path,
    session_id: int,
    message_id: int,
    *,
    keep_assistant_after: bool,
) -> None:
    shutil.copy2(source_db, replay_db)
    with _connect(replay_db) as conn:
        sequence_row = conn.execute(
            "SELECT sequence_no FROM session_messages WHERE session_id = ? AND message_id = ?",
            (session_id, message_id),
        ).fetchone()
        sequence_no = int(sequence_row["sequence_no"]) if sequence_row else None
        if sequence_no is not None and not keep_assistant_after:
            assistant_ids = [
                int(row["message_id"])
                for row in conn.execute(
                    """
                    SELECT sm.message_id
                    FROM session_messages sm
                    JOIN messages m ON m.id = sm.message_id
                    WHERE sm.session_id = ?
                      AND sm.sequence_no > ?
                      AND (COALESCE(sm.role, '') = 'assistant'
                           OR COALESCE(m.sender_id, '') = 'bot'
                           OR COALESCE(m.classified_type, '') = 'bot_reply')
                    """,
                    (session_id, sequence_no),
                ).fetchall()
            ]
            for assistant_id in assistant_ids:
                conn.execute("DELETE FROM session_messages WHERE message_id = ?", (assistant_id,))
                conn.execute("DELETE FROM messages WHERE id = ?", (assistant_id,))

        conn.execute(
            "DELETE FROM audit_logs WHERE target_type = 'message' AND target_id = ?",
            (str(message_id),),
        )
        conn.execute(
            "DELETE FROM agent_runs WHERE session_id = ? AND (message_id = ? OR agent_name LIKE 'dispatch:%')",
            (session_id, message_id),
        )
        conn.execute(
            """
            UPDATE messages
            SET pipeline_status = 'pending',
                pipeline_error = '',
                processed_at = NULL
            WHERE id = ?
            """,
            (message_id,),
        )
        conn.execute(
            """
            UPDATE sessions
            SET status = 'open',
                updated_at = ?
            WHERE id = ?
            """,
            (datetime.now().isoformat(), session_id),
        )
        conn.commit()


def _copy_session_files(source_session: Path, replay_session: Path) -> None:
    if not source_session.exists():
        return
    for name in (
        "uploads",
        "attachments",
        ".triage",
        ".ones",
        ".review",
        "scratch",
    ):
        src = source_session / name
        dst = replay_session / name
        if src.exists() and not dst.exists():
            shutil.copytree(src, dst)
    for name in ("session_workspace.json", "project_workspace.json"):
        src = source_session / name
        if src.exists():
            shutil.copy2(src, replay_session / name)


async def _run_pipeline(
    repo: Path,
    paths: ReplayPaths,
    session_id: int,
    message_id: int,
) -> dict[str, Any]:
    from core.agents.runner import AgentRunner, DefaultAgentPort
    from core.app.message_processor import MessageProcessor
    from core.app.result_handler import ResultHandler
    from core.artifacts.workspace import WorkspacePreparer
    from core.connectors.feishu import FeishuFilePort
    from core.deps import CoreDependencies, SystemClock
    from core.repositories import Repository
    from core.sessions.service import SessionService

    previous_db = os.environ.get("WORK_AGENT_REPLAY_DB_PATH")
    previous_sessions = os.environ.get("WORK_AGENT_REPLAY_SESSIONS_DIR")
    os.environ["WORK_AGENT_REPLAY_DB_PATH"] = str(paths.db_path)
    os.environ["WORK_AGENT_REPLAY_SESSIONS_DIR"] = str(paths.sessions_root)
    try:
        repository = Repository(paths.db_path)
        sessions = SessionService(repository)
        clock = SystemClock()
        mock_channel = MockFeishuChannel()
        agents = AgentRunner(DefaultAgentPort())
        result_handler = ResultHandler(
            repository=repository,
            channel_port=mock_channel,
            clock=clock,
            reply_repairer=agents,
        )
        deps = CoreDependencies(
            repository=repository,
            sessions=sessions,
            workspaces=WorkspacePreparer(FeishuFilePort(), workspace_root=paths.sessions_root),
            agents=agents,
            result_handler=result_handler,
            clock=clock,
        )
        error = ""
        try:
            await MessageProcessor(deps.processor_deps()).process(message_id)
        except Exception as exc:  # noqa: BLE001 - diagnostic replay should report all failures.
            error = f"{type(exc).__name__}: {exc}"
        return {
            "pipeline_error": error,
            "message": _one(
                paths.db_path,
                "SELECT id, session_id, pipeline_status, pipeline_error, processed_at FROM messages WHERE id = ?",
                (message_id,),
            ),
            "session": _one(
                paths.db_path,
                """
                SELECT id, project, thread_id, agent_runtime, agent_session_id,
                       analysis_workspace, updated_at
                FROM sessions WHERE id = ?
                """,
                (session_id,),
            ),
            "agent_runs": _rows(
                paths.db_path,
                """
                SELECT id, agent_name, runtime_type, session_id, message_id, status,
                       started_at, ended_at, error_message
                FROM agent_runs
                WHERE session_id = ?
                ORDER BY id
                """,
                (session_id,),
            ),
            "audit_logs": _rows(
                paths.db_path,
                """
                SELECT id, event_type, target_type, target_id, detail, created_at
                FROM audit_logs
                WHERE target_type = 'message' AND target_id = ?
                ORDER BY id
                """,
                (str(message_id),),
            ),
            "mock_feishu_deliveries": [delivery.__dict__ for delivery in mock_channel.deliveries],
        }
    finally:
        _restore_env("WORK_AGENT_REPLAY_DB_PATH", previous_db)
        _restore_env("WORK_AGENT_REPLAY_SESSIONS_DIR", previous_sessions)


def _restore_env(name: str, value: str | None) -> None:
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value


if __name__ == "__main__":
    raise SystemExit(main())
