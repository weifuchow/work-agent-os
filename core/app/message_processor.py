"""Public message processing use case."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import time
from typing import Any

from loguru import logger

from core.agents.runner import AgentRunner
from core.app.context import MessageContext, PreparedWorkspace
from core.app.contract import SkillResult, parse_skill_result
from core.app.ones_prefetch import extract_ones_reference, prepare_ones_intake
from core.app.project_context import is_direct_project_entry_message, prepare_direct_project_context
from core.app.result_handler import ResultHandler
from core.app.triage_trace import ensure_triage_analysis_traces
from core.artifacts.workspace import WorkspacePreparer
from core.ports import AgentResponse, ClockPort, ReplyPayload
from core.repositories import Repository
from core.sessions.service import SessionService


ATTACHMENT_TOKEN_RE = re.compile(
    r"\[(?:文件|图片|语音|视频|file|image|audio|video)(?:[:：][^\]]*)?\]",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class MessageProcessorDeps:
    repository: Repository
    sessions: SessionService
    workspaces: WorkspacePreparer
    agents: AgentRunner
    result_handler: ResultHandler
    clock: ClockPort


class MessageProcessor:
    def __init__(self, deps: MessageProcessorDeps) -> None:
        self.deps = deps

    async def process(self, message_id: int) -> None:
        msg = await self.deps.repository.read_message(message_id)
        if not msg:
            return
        if str(msg.get("pipeline_status") or "") == "completed":
            return

        now = self.deps.clock.now_iso()
        await self.deps.repository.update_message(
            message_id,
            pipeline_status="processing",
            pipeline_error="",
        )

        session = await self.deps.sessions.resolve_for_message(msg, now=now)
        session_id = int(session["id"]) if session and session.get("id") is not None else None
        await self.deps.repository.audit(
            "message_processing_started",
            target_type="message",
            target_id=str(message_id),
            detail={
                "message_id": message_id,
                "session_id": session_id,
                "attempt": 1,
            },
        )

        lock = await self.deps.sessions.lock_for(session_id)
        if lock is None:
            await self._process_locked(message_id, session)
            return

        async with lock:
            await self._process_locked(message_id, await self.deps.repository.read_session(session_id))

    async def _process_locked(
        self,
        message_id: int,
        session: dict[str, Any] | None,
    ) -> None:
        msg = await self.deps.repository.read_message(message_id)
        if not msg:
            return
        session_id = int(session["id"]) if session and session.get("id") is not None else None
        history = await self.deps.repository.history_for_session(session_id)
        ctx = MessageContext(message=msg, session=session, history=history, attempt=1)

        try:
            workspace = await self.deps.workspaces.prepare(ctx)
            await self.deps.repository.audit(
                "workspace_prepared",
                target_type="message",
                target_id=str(message_id),
                detail={
                    "message_id": message_id,
                    "session_id": session_id,
                    "session_dir": workspace.artifact_roots.get("session_dir", ""),
                    "workspace_path": str(workspace.path),
                    "session_workspace_path": str(
                        Path(workspace.artifact_roots.get("session_dir", "")) / "session_workspace.json"
                    ),
                    "artifact_roots": workspace.artifact_roots,
                },
            )

            if _is_attachment_only_message(msg):
                await self.deps.result_handler.apply(
                    ctx=ctx,
                    workspace=workspace,
                    result=_attachment_ack_result(),
                )
                return

            runtime = str((session or {}).get("agent_runtime") or "")
            has_ones_reference = bool(extract_ones_reference(str(msg.get("content") or "")))
            ones_prefetch = await prepare_ones_intake(ctx, workspace, runtime=runtime)
            if ones_prefetch:
                project_context = (
                    ones_prefetch.project_context
                    if isinstance(getattr(ones_prefetch, "project_context", None), dict)
                    else {}
                )
                running_project = str(project_context.get("running_project") or "").strip()
                if session_id and running_project:
                    await self.deps.repository.update_session_patch(
                        session_id,
                        {
                            "project": running_project,
                            "analysis_workspace": str(workspace.artifact_roots.get("session_dir") or workspace.path.parent),
                        },
                        now=self.deps.clock.now_iso(),
                    )
                    ctx = MessageContext(
                        message=ctx.message,
                        session={
                            **(ctx.session or {}),
                            "project": running_project,
                            "analysis_workspace": str(workspace.artifact_roots.get("session_dir") or workspace.path.parent),
                        },
                        history=ctx.history,
                        attempt=ctx.attempt,
                    )
                await self.deps.repository.audit(
                    "ones_intake_prepared",
                    target_type="message",
                    target_id=str(message_id),
                    detail={
                        "message_id": message_id,
                        "session_id": session_id,
                        **ones_prefetch.to_audit_detail(),
                    },
                )

            if not has_ones_reference and not ones_prefetch:
                direct_project_context = prepare_direct_project_context(ctx, workspace)
                if direct_project_context:
                    if session_id:
                        await self.deps.repository.update_session_patch(
                            session_id,
                            {
                                "project": str(direct_project_context.get("running_project") or ""),
                                "analysis_workspace": str(workspace.artifact_roots.get("session_dir") or workspace.path.parent),
                            },
                            now=self.deps.clock.now_iso(),
                        )
                        ctx = MessageContext(
                            message=ctx.message,
                            session={
                                **(ctx.session or {}),
                                "project": str(direct_project_context.get("running_project") or ""),
                            },
                            history=ctx.history,
                            attempt=ctx.attempt,
                        )
                    if is_direct_project_entry_message(ctx, direct_project_context):
                        await self.deps.repository.audit(
                            "project_entry_fast_path",
                            target_type="message",
                            target_id=str(message_id),
                            detail={
                                "message_id": message_id,
                                "session_id": session_id,
                                "running_project": direct_project_context.get("running_project") or "",
                                "execution_path": direct_project_context.get("execution_path") or "",
                            },
                        )
                        await self.deps.result_handler.apply(
                            ctx=ctx,
                            workspace=workspace,
                            result=_project_entry_result(direct_project_context),
                        )
                        return
                    await self.deps.repository.audit(
                        "project_workspace_prepared",
                        target_type="message",
                        target_id=str(message_id),
                        detail={
                            "message_id": message_id,
                            "session_id": session_id,
                            "reason": "direct_project_alias",
                            "running_project": direct_project_context.get("running_project") or "",
                            "project_path": direct_project_context.get("project_path") or "",
                            "current_branch": direct_project_context.get("current_branch") or "",
                            "current_version": direct_project_context.get("current_version") or "",
                        },
                    )

            await self.deps.repository.audit(
                "agent_run_started",
                target_type="message",
                target_id=str(message_id),
                detail={
                    "message_id": message_id,
                    "session_id": session_id,
                    "workspace_path": str(workspace.path),
                    "agent_runtime": runtime,
                },
            )

            started_at = self.deps.clock.now_iso()
            artifact_not_before = time.time()
            recovered_from_agent_error = ""
            recovered_artifact_path = ""
            try:
                result, response = await self.deps.agents.run_with_skills(ctx, workspace)
            except Exception as exc:
                recovered = _recover_reply_contract_from_workspace(
                    ctx,
                    workspace,
                    not_before_mtime=artifact_not_before,
                )
                if not recovered:
                    raise
                result, recovered_artifact_path = recovered
                recovered_from_agent_error = str(exc)
                response = AgentResponse(
                    text=json.dumps(result.raw.get("parsed") or {}, ensure_ascii=False),
                    runtime=runtime,
                    raw={
                        "recovered_from_artifact": recovered_artifact_path,
                        "agent_error": recovered_from_agent_error,
                    },
                )
                await self.deps.repository.audit(
                    "agent_run_recovered_from_artifact",
                    target_type="message",
                    target_id=str(message_id),
                    detail={
                        "message_id": message_id,
                        "session_id": session_id,
                        "artifact_path": recovered_artifact_path,
                        "agent_error": recovered_from_agent_error[:1000],
                        "action": result.action,
                    },
                )
            ended_at = self.deps.clock.now_iso()
            result = _stamp_reply_artifact_not_before(result, artifact_not_before)
            trace_paths = ensure_triage_analysis_traces(workspace)
            if trace_paths:
                await self.deps.repository.audit(
                    "triage_analysis_trace_written",
                    target_type="message",
                    target_id=str(message_id),
                    detail={
                        "message_id": message_id,
                        "session_id": session_id,
                        "trace_paths": [str(path) for path in trace_paths],
                    },
                )
            await self.deps.repository.audit(
                "agent_run_completed",
                target_type="message",
                target_id=str(message_id),
                detail={
                    "message_id": message_id,
                    "session_id": session_id,
                    "action": result.action,
                    "skill_trace": result.skill_trace,
                },
            )
            await self.deps.repository.record_agent_run(
                message_id=message_id,
                session_id=session_id,
                runtime_type=response.runtime or runtime,
                status="success" if result.action != "failed" else "failed",
                started_at=started_at,
                ended_at=ended_at,
                input_path=str(workspace.path / "input"),
                output_path=str(workspace.path / "output"),
                input_tokens=_int_token(response.usage, "input_tokens"),
                output_tokens=_int_token(response.usage, "output_tokens"),
                cost_usd=_float_value(response.raw.get("cost_usd")),
                error_message=(
                    result.error_message
                    or (f"recovered after agent error: {recovered_from_agent_error[:400]}" if recovered_from_agent_error else "")
                ),
            )
            await self.deps.result_handler.apply(ctx=ctx, workspace=workspace, result=result)
        except Exception as exc:
            logger.exception("Message processing failed for {}: {}", message_id, exc)
            await self._fail_message(
                message_id=message_id,
                session_id=session_id,
                failed_stage="message_processor",
                error=exc,
            )

    async def _fail_message(
        self,
        *,
        message_id: int,
        session_id: int | None,
        failed_stage: str,
        error: Exception,
    ) -> None:
        error_message = str(error)
        now = self.deps.clock.now_iso()
        await self.deps.repository.audit(
            "agent_run_failed",
            target_type="message",
            target_id=str(message_id),
            detail={
                "message_id": message_id,
                "session_id": session_id,
                "error_type": type(error).__name__,
                "error_message": error_message[:1000],
            },
        )
        await self.deps.repository.update_message(
            message_id,
            pipeline_status="failed",
            pipeline_error=error_message[:500],
            processed_at=now,
        )
        await self.deps.repository.audit(
            "message_failed",
            target_type="message",
            target_id=str(message_id),
            detail={
                "message_id": message_id,
                "session_id": session_id,
                "failed_stage": failed_stage,
                "error_message": error_message[:1000],
            },
        )


def _int_token(usage: dict[str, Any], key: str) -> int:
    try:
        return int(usage.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _float_value(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _recover_reply_contract_from_workspace(
    ctx: MessageContext,
    workspace: PreparedWorkspace,
    *,
    not_before_mtime: float = 0.0,
) -> tuple[SkillResult, str] | None:
    """Recover a finished reply when the agent process fails after writing artifacts."""

    candidates = _reply_contract_candidates(ctx, workspace)
    for path in candidates:
        if not _is_recoverable_reply_contract(path, ctx=ctx, not_before_mtime=not_before_mtime):
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            logger.warning("Skipping unreadable reply contract artifact {}: {}", path, exc)
            continue
        result = parse_skill_result(payload)
        if result.action == "reply" and result.reply is not None:
            return result, str(path)
    return None


def _stamp_reply_artifact_not_before(result: SkillResult, not_before_mtime: float) -> SkillResult:
    if result.action != "reply" or result.reply is None or not not_before_mtime:
        return result
    reply = result.reply
    metadata = dict(reply.metadata)
    metadata.setdefault("attachment_not_before_mtime", not_before_mtime)
    return SkillResult(
        action=result.action,
        reply=ReplyPayload(
            channel=reply.channel,
            type=reply.type,
            content=reply.content,
            payload=reply.payload,
            intent=reply.intent,
            file_path=reply.file_path,
            metadata=metadata,
        ),
        session_patch=result.session_patch,
        workspace_patch=result.workspace_patch,
        skill_trace=result.skill_trace,
        audit=result.audit,
        error_message=result.error_message,
        raw=result.raw,
    )


def _is_recoverable_reply_contract(
    path: Path,
    *,
    ctx: MessageContext,
    not_before_mtime: float,
) -> bool:
    if not not_before_mtime:
        return True
    mtime = _mtime(path)
    if path.name == f"reply_contract_{ctx.message_id}.json":
        return mtime + 2.0 >= not_before_mtime
    return mtime >= not_before_mtime


def _reply_contract_candidates(ctx: MessageContext, workspace: PreparedWorkspace) -> list[Path]:
    output_dir = workspace.output_dir
    message_id = str(ctx.message_id)
    preferred = [
        output_dir / f"reply_contract_{message_id}.json",
        output_dir / "reply_contract.json",
    ]
    candidates: list[Path] = []
    seen: set[Path] = set()
    for path in [*preferred, *sorted(output_dir.glob("reply_contract*.json"), key=_mtime, reverse=True)]:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved in seen or not path.is_file():
            continue
        seen.add(resolved)
        candidates.append(path)
    return candidates


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _is_attachment_only_message(msg: dict[str, Any]) -> bool:
    message_type = str(msg.get("message_type") or "").strip().lower()
    if message_type in {"", "text", "interactive", "sticker", "share_chat", "share_user"}:
        return False

    has_media = bool(msg.get("attachment_path") or msg.get("media_info_json"))
    if not has_media and message_type not in {"file", "image", "audio", "video", "post"}:
        return False

    content = str(msg.get("content") or "").strip()
    if not content:
        return True
    content_without_tokens = ATTACHMENT_TOKEN_RE.sub("", content).strip()
    return not content_without_tokens


def _attachment_ack_result() -> SkillResult:
    return SkillResult(
        action="reply",
        reply=ReplyPayload(
            channel="feishu",
            type="markdown",
            content=(
                "已收到附件，已归档到本 session 的 `uploads`。"
                "我先不分析内容，请继续发送要分析的问题、时间、订单或车辆信息。"
            ),
            intent="needs_input",
        ),
        skill_trace=[{"skill": "core-attachment-ack", "reason": "attachment_only_message"}],
        audit=[
            {
                "event_type": "attachment_only_message_acknowledged",
                "detail": {
                    "reason": "message contains only attachment placeholders; agent run skipped",
                },
            }
        ],
    )


def _project_entry_result(runtime_context: dict[str, Any]) -> SkillResult:
    project = str(runtime_context.get("running_project") or "项目").strip()
    source_path = str(runtime_context.get("source_path") or runtime_context.get("project_path") or "").strip()
    worktree_path = str(runtime_context.get("worktree_path") or runtime_context.get("execution_path") or "").strip()
    version = str(
        runtime_context.get("execution_version")
        or runtime_context.get("current_version")
        or runtime_context.get("execution_describe")
        or runtime_context.get("current_describe")
        or ""
    ).strip()
    summary = f"已进入 {project} 项目上下文，后续问题会优先基于 session worktree 处理。"
    return SkillResult(
        action="reply",
        reply=ReplyPayload(
            channel="feishu",
            type="feishu_card",
            content=summary,
            payload={
                "schema": "2.0",
                "config": {"update_multi": True},
                "header": {
                    "template": "blue",
                    "title": {"tag": "plain_text", "content": f"已进入 {project}"},
                },
                "body": {
                    "elements": [
                        {"tag": "markdown", "content": summary, "text_align": "left", "text_size": "normal"},
                        {
                            "tag": "table",
                            "columns": [
                                {"name": "name", "display_name": "信息", "data_type": "text"},
                                {"name": "value", "display_name": "值", "data_type": "text"},
                            ],
                            "rows": [
                                {"name": "项目", "value": project},
                                {"name": "源仓库", "value": source_path or "未配置"},
                                {"name": "Worktree", "value": worktree_path or "未准备"},
                                {"name": "版本", "value": version or "未识别"},
                            ],
                        },
                        {
                            "tag": "markdown",
                            "content": "可以继续发送具体问题、版本、日志、截图或 ONES 链接。",
                            "text_align": "left",
                            "text_size": "normal",
                        },
                    ]
                },
            },
            intent="project_context_ready",
        ),
        session_patch={
            "project": project,
            "analysis_workspace": str(runtime_context.get("session_dir") or ""),
        },
        skill_trace=[{"skill": "core-project-entry", "reason": "direct project name first message"}],
        audit=[
            {
                "event_type": "project_entry_acknowledged",
                "detail": {
                    "running_project": project,
                    "execution_path": worktree_path,
                    "version": version,
                },
            }
        ],
    )
