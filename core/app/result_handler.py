"""Apply generic agent results to DB and channel adapters."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import re
from typing import Any

from core.connectors.feishu import _MERMAID_BLOCK_RE, _render_mermaid_png, _sanitize_feishu_card_payload
from core.app.context import MessageContext, PreparedWorkspace
from core.app.contract import SkillResult
from core.app.reply_enrichment import (
    attach_declared_feishu_reply_files,
    enhance_feishu_card_code_blocks,
    enrich_reply_with_workspace_context,
)
from core.ports import ChannelPort, ClockPort, ReplyPayload, ReplyRepairPort
from core.repositories import Repository


class ResultHandler:
    def __init__(
        self,
        *,
        repository: Repository,
        channel_port: ChannelPort,
        clock: ClockPort,
        reply_repairer: ReplyRepairPort | None = None,
    ) -> None:
        self.repository = repository
        self.channel_port = channel_port
        self.clock = clock
        self.reply_repairer = reply_repairer

    async def apply(
        self,
        *,
        ctx: MessageContext,
        workspace: PreparedWorkspace,
        result: SkillResult,
    ) -> None:
        for event in result.audit:
            event_type = str(event.get("event_type") or "agent_audit")
            detail = event.get("detail") if isinstance(event.get("detail"), dict) else event
            await self.repository.audit(
                event_type,
                target_type="message",
                target_id=str(ctx.message_id),
                detail=detail,
            )

        if result.session_patch and ctx.session_id:
            await self.repository.update_session_patch(
                ctx.session_id,
                result.session_patch,
                now=self.clock.now_iso(),
            )

        if result.action == "failed":
            await self._mark_failed(
                ctx,
                failed_stage="agent_result",
                error_message=result.error_message or "agent returned action=failed",
            )
            return

        if result.action == "no_reply":
            await self._mark_completed(ctx, action="no_reply")
            return

        if result.action == "reply":
            await self._deliver_reply(ctx, workspace, result.reply)
            return

        await self._mark_failed(
            ctx,
            failed_stage="result_handler",
            error_message=f"unsupported action: {result.action}",
        )

    async def _deliver_reply(
        self,
        ctx: MessageContext,
        workspace: PreparedWorkspace,
        reply: ReplyPayload | None,
    ) -> None:
        if reply is None or not reply.type:
            await self._mark_failed(
                ctx,
                failed_stage="reply_validation",
                error_message="reply payload is missing",
            )
            return
        reply = self._rebuild_card_from_structured_summary(ctx, workspace, reply)
        reply = enhance_feishu_card_code_blocks(reply)
        reply = enrich_reply_with_workspace_context(reply, workspace)
        reply = await self._repair_reply_if_needed(ctx, workspace, reply)
        reply = enhance_feishu_card_code_blocks(reply)
        reply = enrich_reply_with_workspace_context(reply, workspace)
        reply = attach_declared_feishu_reply_files(reply, workspace)
        if reply.type in {"text", "markdown"} and not reply.content.strip():
            await self._mark_failed(
                ctx,
                failed_stage="reply_validation",
                error_message="reply content is empty",
            )
            return

        await self.repository.audit(
            "reply_delivery_started",
            target_type="message",
            target_id=str(ctx.message_id),
            detail={
                "message_id": ctx.message_id,
                "session_id": ctx.session_id,
                "reply_type": reply.type,
                "channel": reply.channel,
                "source_thread_id": ctx.message.get("thread_id") or "",
                "source_root_id": ctx.message.get("root_id") or "",
                "source_parent_id": ctx.message.get("parent_id") or "",
            },
        )

        delivery = await self.channel_port.deliver_reply(
            source_message=ctx.message,
            reply=reply,
        )
        if not delivery.delivered:
            await self.repository.audit(
                "reply_delivery_failed",
                target_type="message",
                target_id=str(ctx.message_id),
                detail={
                    "message_id": ctx.message_id,
                    "session_id": ctx.session_id,
                    "error_type": "delivery_failed",
                    "error_message": delivery.error,
                },
            )
            fallback_reply = _delivery_fallback_reply(reply)
            if fallback_reply is not None:
                await self.repository.audit(
                    "reply_delivery_fallback_started",
                    target_type="message",
                    target_id=str(ctx.message_id),
                    detail={
                        "message_id": ctx.message_id,
                        "session_id": ctx.session_id,
                        "from_reply_type": reply.type,
                        "to_reply_type": fallback_reply.type,
                        "error_message": delivery.error,
                    },
                )
                fallback_delivery = await self.channel_port.deliver_reply(
                    source_message=ctx.message,
                    reply=fallback_reply,
                )
                if fallback_delivery.delivered:
                    await self.repository.audit(
                        "reply_delivery_fallback_completed",
                        target_type="message",
                        target_id=str(ctx.message_id),
                        detail={
                            "message_id": ctx.message_id,
                            "session_id": ctx.session_id,
                            "reply_type": fallback_reply.type,
                            "original_error": delivery.error,
                        },
                    )
                    await self._save_successful_delivery(
                        ctx,
                        workspace,
                        fallback_reply,
                        fallback_delivery,
                        delivery_mode="fallback",
                        original_error=delivery.error,
                    )
                    return
                await self.repository.audit(
                    "reply_delivery_fallback_failed",
                    target_type="message",
                    target_id=str(ctx.message_id),
                    detail={
                        "message_id": ctx.message_id,
                        "session_id": ctx.session_id,
                        "error_type": "delivery_failed",
                        "error_message": fallback_delivery.error,
                        "original_error": delivery.error,
                    },
                )
            await self._mark_failed(
                ctx,
                failed_stage="reply_delivery",
                error_message=delivery.error or "reply delivery failed",
            )
            return

        await self._save_successful_delivery(ctx, workspace, reply, delivery)

    async def _save_successful_delivery(
        self,
        ctx: MessageContext,
        workspace: PreparedWorkspace,
        reply: ReplyPayload,
        delivery: Any,
        *,
        delivery_mode: str = "primary",
        original_error: str = "",
    ) -> None:

        now = self.clock.now_iso()
        session_patch: dict[str, Any] = {}
        if delivery.thread_id:
            session_patch["thread_id"] = delivery.thread_id
        if session_patch and ctx.session_id:
            await self.repository.update_session_patch(ctx.session_id, session_patch, now=now)

        await self.repository.save_bot_reply(
            source_message=ctx.message,
            session_id=ctx.session_id,
            reply_content=reply.content,
            reply_type=reply.type,
            raw_payload={
                "reply": reply.to_dict(),
                "delivery": {
                    "message_id": delivery.message_id,
                    "thread_id": delivery.thread_id,
                    "root_id": delivery.root_id,
                    "mode": delivery_mode,
                    "original_error": original_error,
                },
                "workspace_path": str(workspace.path),
            },
            now=now,
        )
        await self._mark_completed(
            ctx,
            action="reply",
            detail={
                "reply_message_id": delivery.message_id,
                "reply_thread_id": delivery.thread_id,
                "reply_root_id": delivery.root_id,
                "delivery_mode": delivery_mode,
            },
        )

    def _rebuild_card_from_structured_summary(
        self,
        ctx: MessageContext,
        workspace: PreparedWorkspace,
        reply: ReplyPayload,
    ) -> ReplyPayload:
        if reply.type == "feishu_card":
            return reply
        summary_path = workspace.output_dir / f"structured_summary_{ctx.message_id}.json"
        if not summary_path.exists():
            return reply
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return reply
        if not isinstance(summary, dict):
            return reply
        renderer = _load_feishu_card_renderer()
        if not renderer:
            return reply
        try:
            contract = renderer.render_contract(summary, reply_type="feishu_card")
        except Exception:
            return reply
        rendered = contract.get("reply") if isinstance(contract, dict) else None
        if not isinstance(rendered, dict):
            return reply
        rebuilt = ReplyPayload.from_dict(rendered)
        if reply.intent and not rebuilt.intent:
            rebuilt = ReplyPayload(
                channel=rebuilt.channel,
                type=rebuilt.type,
                content=rebuilt.content,
                payload=rebuilt.payload,
                intent=reply.intent,
                file_path=rebuilt.file_path,
                metadata=dict(rebuilt.metadata),
        )
        return rebuilt

    async def _repair_reply_if_needed(
        self,
        ctx: MessageContext,
        workspace: PreparedWorkspace,
        reply: ReplyPayload,
    ) -> ReplyPayload:
        errors = _reply_validation_errors(reply)
        if not errors:
            return reply

        await self.repository.audit(
            "reply_validation_failed",
            target_type="message",
            target_id=str(ctx.message_id),
            detail={
                "message_id": ctx.message_id,
                "session_id": ctx.session_id,
                "reply_type": reply.type,
                "errors": errors,
            },
        )
        if self.reply_repairer is None:
            return reply

        await self.repository.audit(
            "reply_repair_started",
            target_type="message",
            target_id=str(ctx.message_id),
            detail={
                "message_id": ctx.message_id,
                "session_id": ctx.session_id,
                "error_count": len(errors),
            },
        )
        try:
            repaired = await self.reply_repairer.repair_reply(
                ctx=ctx,
                workspace=workspace,
                reply=reply,
                validation_errors=errors,
            )
        except Exception as exc:
            await self.repository.audit(
                "reply_repair_failed",
                target_type="message",
                target_id=str(ctx.message_id),
                detail={
                    "message_id": ctx.message_id,
                    "session_id": ctx.session_id,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc)[:1000],
                },
            )
            return reply

        if repaired is None:
            await self.repository.audit(
                "reply_repair_failed",
                target_type="message",
                target_id=str(ctx.message_id),
                detail={
                    "message_id": ctx.message_id,
                    "session_id": ctx.session_id,
                    "error_message": "model returned no usable repaired reply",
                },
            )
            return reply

        repaired = enrich_reply_with_workspace_context(repaired, workspace)
        remaining_errors = _reply_validation_errors(repaired)
        if remaining_errors:
            await self.repository.audit(
                "reply_repair_failed",
                target_type="message",
                target_id=str(ctx.message_id),
                detail={
                    "message_id": ctx.message_id,
                    "session_id": ctx.session_id,
                    "error_message": "repaired reply did not pass validation",
                    "errors": remaining_errors,
                },
            )
            return reply

        await self.repository.audit(
            "reply_repair_completed",
            target_type="message",
            target_id=str(ctx.message_id),
            detail={
                "message_id": ctx.message_id,
                "session_id": ctx.session_id,
                "reply_type": repaired.type,
            },
        )
        return repaired

    async def _mark_completed(
        self,
        ctx: MessageContext,
        *,
        action: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        now = self.clock.now_iso()
        await self.repository.update_message(
            ctx.message_id,
            pipeline_status="completed",
            pipeline_error="",
            processed_at=now,
        )
        await self.repository.audit(
            "message_completed",
            target_type="message",
            target_id=str(ctx.message_id),
            detail={
                "message_id": ctx.message_id,
                "session_id": ctx.session_id,
                "action": action,
                **(detail or {}),
            },
        )

    async def _mark_failed(
        self,
        ctx: MessageContext,
        *,
        failed_stage: str,
        error_message: str,
    ) -> None:
        now = self.clock.now_iso()
        await self.repository.update_message(
            ctx.message_id,
            pipeline_status="failed",
            pipeline_error=error_message[:500],
            processed_at=now,
        )
        await self.repository.audit(
            "message_failed",
            target_type="message",
            target_id=str(ctx.message_id),
            detail={
                "message_id": ctx.message_id,
                "session_id": ctx.session_id,
                "failed_stage": failed_stage,
                "error_message": error_message[:1000],
            },
        )


def _load_feishu_card_renderer():
    path = (
        Path(__file__).resolve().parents[2]
        / ".claude"
        / "skills"
        / "feishu_card_builder"
        / "scripts"
        / "render_feishu_card.py"
    )
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location("_work_agent_feishu_card_renderer", path)
    if not spec or not spec.loader:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _delivery_fallback_reply(reply: ReplyPayload) -> ReplyPayload | None:
    if reply.type in {"text", "markdown"}:
        return None
    content = _plain_reply_text(reply)
    if not content.strip():
        return None
    return ReplyPayload(
        channel=reply.channel,
        type="markdown",
        content=content,
        payload=None,
        intent=reply.intent,
        file_path=reply.file_path,
        metadata={**dict(reply.metadata), "delivery_fallback_from": reply.type},
    )


def _plain_reply_text(reply: ReplyPayload) -> str:
    content = str(reply.content or "").strip()
    if content:
        return content
    if isinstance(reply.payload, dict):
        summary = reply.payload.get("structured_summary")
        if isinstance(summary, dict):
            return _summary_to_markdown(summary)
        return _card_to_markdown(reply.payload)
    return ""


def _summary_to_markdown(summary: dict[str, Any]) -> str:
    parts: list[str] = []
    title = str(summary.get("title") or "").strip()
    if title:
        parts.append(f"**{title}**")
    for key in ("summary", "fallback_text", "conclusion"):
        value = str(summary.get(key) or "").strip()
        if value:
            parts.append(value)
            break
    sections = summary.get("sections")
    if isinstance(sections, list):
        for section in sections[:6]:
            if not isinstance(section, dict):
                continue
            heading = str(section.get("title") or "").strip()
            body = str(section.get("content") or section.get("body") or "").strip()
            if heading and body:
                parts.append(f"**{heading}**\n{body}")
            elif body:
                parts.append(body)
    return "\n\n".join(parts).strip()


def _card_to_markdown(payload: dict[str, Any]) -> str:
    card = _sanitize_feishu_card_payload(payload)
    if not card:
        return ""
    parts: list[str] = []
    header = card.get("header")
    if isinstance(header, dict):
        title = header.get("title")
        if isinstance(title, dict):
            value = str(title.get("content") or "").strip()
            if value:
                parts.append(f"**{value}**")
    body = card.get("body")
    elements = body.get("elements") if isinstance(body, dict) else None
    if isinstance(elements, list):
        for text in _iter_card_text(elements):
            if text:
                parts.append(text)
    return "\n\n".join(parts).strip()


def _iter_card_text(value: Any) -> list[str]:
    texts: list[str] = []
    if isinstance(value, list):
        for item in value:
            texts.extend(_iter_card_text(item))
        return texts
    if not isinstance(value, dict):
        return texts
    tag = value.get("tag")
    if tag in {"markdown", "plain_text", "lark_md"}:
        text = str(value.get("content") or value.get("text") or "").strip()
        if text:
            texts.append(_strip_markdown_images(text))
    text_obj = value.get("text")
    if isinstance(text_obj, dict):
        text = str(text_obj.get("content") or "").strip()
        if text:
            texts.append(_strip_markdown_images(text))
    for key in ("elements", "columns"):
        nested = value.get(key)
        if isinstance(nested, list):
            texts.extend(_iter_card_text(nested))
    return texts


def _strip_markdown_images(text: str) -> str:
    return re.sub(r"!\[[^\]]*\]\([^)]+\)", "", text).strip()


def _reply_validation_errors(reply: ReplyPayload) -> list[dict[str, Any]]:
    if reply.type != "feishu_card":
        return []

    payload = _sanitize_feishu_card_payload(reply.payload)
    if not payload:
        return [
            {
                "type": "invalid_feishu_card_payload",
                "message": "reply.type is feishu_card but payload is not a valid Feishu card",
            }
        ]

    errors: list[dict[str, Any]] = []
    for index, source in enumerate(_iter_mermaid_sources(payload), start=1):
        if _render_mermaid_png(source) is None:
            errors.append(
                {
                    "type": "mermaid_render_failed",
                    "index": index,
                    "message": "Mermaid block could not be rendered by mermaid-cli",
                    "source_preview": source[:1000],
                }
            )
    return errors


def _iter_mermaid_sources(payload: dict[str, Any]) -> list[str]:
    body = payload.get("body")
    if not isinstance(body, dict):
        return []
    elements = body.get("elements")
    if not isinstance(elements, list):
        return []

    sources: list[str] = []
    for element in elements:
        if not isinstance(element, dict):
            continue
        if element.get("tag") != "markdown":
            continue
        content = str(element.get("content") or "")
        for match in _MERMAID_BLOCK_RE.finditer(content):
            source = match.group(1).strip()
            if source:
                sources.append(source)
    return sources
