"""Prepare project runtime context for directly mentioned projects."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from core.app.context import MessageContext, PreparedWorkspace
from core.projects import get_projects, resolve_project_runtime_context


def prepare_direct_project_context(
    ctx: MessageContext,
    workspace: PreparedWorkspace,
) -> dict[str, Any] | None:
    project_name = _infer_project_name(ctx)
    if not project_name:
        return None

    runtime_context = resolve_project_runtime_context(
        project_name,
        worktree_root=Path(
            workspace.artifact_roots.get("worktrees_dir")
            or (Path(workspace.artifact_roots["session_dir"]) / "worktrees")
        ),
    )
    if not runtime_context:
        return None

    payload = runtime_context.to_payload()
    _persist_project_runtime_context(workspace, payload)
    return payload


def is_direct_project_entry_message(
    ctx: MessageContext,
    runtime_context: dict[str, Any],
) -> bool:
    project_name = str(runtime_context.get("running_project") or "").strip()
    if not project_name:
        return False
    if _user_message_count(ctx) > 1:
        return False

    content = str(ctx.message.get("content") or "").strip()
    if not content or len(content) > 80 or "\n" in content:
        return False

    project = next((item for item in get_projects() if item.name == project_name), None)
    if not project:
        return False
    aliases = [project.name, *_description_aliases(project.description)]
    normalized_content = _normalize_entry_text(content)
    return normalized_content in {_normalize_entry_text(alias) for alias in aliases if alias}


def _infer_project_name(ctx: MessageContext) -> str:
    session_project = str((ctx.session or {}).get("project") or "").strip()
    if session_project:
        return session_project

    text = _project_signal_text(ctx)
    if not text:
        return ""
    for project in get_projects():
        names = [project.name, *_description_aliases(project.description)]
        if any(_contains_project_alias(text, alias) for alias in names):
            return project.name
    return ""


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


def _persist_project_runtime_context(
    workspace: PreparedWorkspace,
    runtime_context: dict[str, Any],
) -> None:
    context_path = workspace.input_dir / "project_context.json"
    try:
        project_context = json.loads(context_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        project_context = {"artifact_roots": workspace.artifact_roots}
    if not isinstance(project_context, dict):
        project_context = {"artifact_roots": workspace.artifact_roots}

    project_context["project_runtime"] = runtime_context
    project_context["project_runtime_context_path"] = str(
        (workspace.input_dir / "project_runtime_context.json").resolve()
    )
    context_path.write_text(
        json.dumps(project_context, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (workspace.input_dir / "project_runtime_context.json").write_text(
        json.dumps(runtime_context, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (workspace.output_dir / "project_runtime_context.json").write_text(
        json.dumps(runtime_context, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
