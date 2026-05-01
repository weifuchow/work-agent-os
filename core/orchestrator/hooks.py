"""Hooks used by Claude Agent SDK runs."""

from __future__ import annotations

from pathlib import Path

from claude_agent_sdk import HookContext, SubagentStopHookInput
from loguru import logger

from core.orchestrator.agent_runtime import get_agent_run_runtime_type

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

async def _on_subagent_stop(
    hook_input: SubagentStopHookInput,
    _progress: str | None,
    _context: HookContext,
) -> dict:
    """Capture sub-agent transcripts when they complete."""
    import aiosqlite
    from datetime import datetime, UTC
    from pathlib import Path

    agent_id = hook_input.agent_id
    agent_type = hook_input.agent_type
    transcript_path = hook_input.agent_transcript_path

    logger.info("SubagentStop: agent={} type={} transcript={}",
                agent_id, agent_type, transcript_path)

    # Read transcript if available
    transcript_content = ""
    if transcript_path:
        try:
            tp = Path(transcript_path)
            if tp.exists():
                transcript_content = tp.read_text(encoding="utf-8")[:10000]
        except Exception as e:
            logger.warning("Failed to read transcript {}: {}", transcript_path, e)

    # Save to agent_runs table
    db_path = PROJECT_ROOT / "data" / "db" / "app.sqlite"
    try:
        async with aiosqlite.connect(str(db_path)) as db:
            now = datetime.now().isoformat()
            await db.execute(
                "INSERT INTO agent_runs (agent_name, runtime_type, input_path, output_path, "
                "status, started_at, ended_at) VALUES (?,?,?,?,?,?,?)",
                (f"subagent:{agent_type}", get_agent_run_runtime_type("claude"), transcript_path or "",
                 transcript_content[:2000], "success", now, now),
            )
            await db.commit()
    except Exception as e:
        logger.warning("Failed to save subagent run: {}", e)

    return {"continue_": True}
