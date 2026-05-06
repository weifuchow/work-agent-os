"""Prepare project runtime context for directly mentioned projects."""

from __future__ import annotations

import re
from typing import Any

from core.app.context import MessageContext, PreparedWorkspace
from core.app.project_workspace import prepare_project_in_workspace
from core.projects import get_projects


def prepare_direct_project_context(
    ctx: MessageContext,
    workspace: PreparedWorkspace,
) -> dict[str, Any] | None:
    project_name = _infer_project_name(ctx)
    if not project_name:
        return None

    payload = prepare_project_in_workspace(
        workspace,
        project_name,
        reason="direct_project_alias",
        active=True,
        worktree_token=_direct_entry_worktree_token(ctx, project_name),
    )
    return payload


def is_direct_project_entry_message(
    ctx: MessageContext,
    runtime_context: dict[str, Any],
) -> bool:
    project_name = str(runtime_context.get("running_project") or "").strip()
    if not project_name:
        return False

    content = str(ctx.message.get("content") or "").strip()
    if not content or len(content) > 80 or "\n" in content:
        return False

    project = next((item for item in get_projects() if item.name == project_name), None)
    if not project:
        return False
    return _is_direct_project_entry_text(content, project)


def _infer_project_name(ctx: MessageContext) -> str:
    message_project = _infer_project_name_from_text(str(ctx.message.get("content") or ""))
    if message_project:
        return message_project

    session_project = str((ctx.session or {}).get("project") or "").strip()
    if session_project:
        return ""

    return _infer_project_name_from_text(_project_signal_text(ctx))


def _direct_entry_worktree_token(ctx: MessageContext, project_name: str) -> str:
    project = next((item for item in get_projects() if item.name == project_name), None)
    if not project:
        return ""
    if not _is_direct_project_entry_text(str(ctx.message.get("content") or ""), project):
        return ""
    message_id = str(ctx.message.get("id") or "").strip()
    if message_id:
        safe_message_id = re.sub(r"[^A-Za-z0-9._-]+", "-", message_id).strip("-")
        if safe_message_id:
            return f"entry-{safe_message_id}"
    return ""


def _infer_project_name_from_text(text: str) -> str:
    if not text:
        return ""
    normalized_text = text.lower()
    matches: list[tuple[int, str]] = []
    for project in get_projects():
        names = [project.name, *_description_aliases(project.description)]
        for alias in names:
            if not _contains_project_alias(normalized_text, alias):
                continue
            normalized_alias = _normalize_entry_text(alias)
            score = len(normalized_alias)
            if _normalize_entry_text(text) == normalized_alias:
                score += 1000
            matches.append((score, project.name))
    if not matches:
        return ""
    return max(matches, key=lambda item: item[0])[1]


def _user_message_count(ctx: MessageContext) -> int:
    count = 0
    for item in ctx.history:
        if not isinstance(item, dict):
            continue
        if str(item.get("sender_id") or "") == "bot":
            continue
        if str(item.get("classified_type") or "") == "bot_reply":
            continue
        count += 1
    return count


def _normalize_entry_text(value: str) -> str:
    return re.sub(r"[\s`'\"“”‘’。.!！?？,，:：;；/\\_-]+", "", str(value or "").lower())


def _is_direct_project_entry_text(content: str, project: Any) -> bool:
    aliases = [project.name, *_description_aliases(project.description)]
    normalized_content = _normalize_entry_text(content)
    return normalized_content in {_normalize_entry_text(alias) for alias in aliases if alias}


def _project_signal_text(ctx: MessageContext) -> str:
    parts = [
        str(ctx.message.get("content") or ""),
        str((ctx.session or {}).get("title") or ""),
        str((ctx.session or {}).get("topic") or ""),
    ]
    for item in ctx.history[-8:]:
        if isinstance(item, dict):
            parts.append(str(item.get("content") or ""))
    return "\n".join(part for part in parts if part.strip()).lower()


def _description_aliases(description: str) -> list[str]:
    aliases: list[str] = []
    for match in re.finditer(r"别名[:：]([^。\n]+)", description or ""):
        raw = match.group(1)
        for item in re.split(r"[、,，/]+", raw):
            alias = item.strip(" `。；;")
            if alias:
                aliases.append(alias)
    return aliases


def _contains_project_alias(text: str, alias: str) -> bool:
    alias_text = str(alias or "").strip().lower()
    if not alias_text:
        return False
    if re.fullmatch(r"[a-z0-9_.-]+", alias_text):
        return bool(re.search(rf"(?<![a-z0-9_.-]){re.escape(alias_text)}(?![a-z0-9_.-])", text))
    return alias_text in text
