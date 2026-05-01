"""Reply enrichment based on prepared workspace context."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.app.context import PreparedWorkspace
from core.ports import ReplyPayload


def enrich_reply_with_workspace_context(
    reply: ReplyPayload,
    workspace: PreparedWorkspace,
) -> ReplyPayload:
    runtime_context = _load_project_runtime_context(workspace)
    if not runtime_context:
        return reply
    if reply.type != "feishu_card" or not isinstance(reply.payload, dict):
        return reply

    block = _runtime_context_markdown(runtime_context)
    if not block:
        return reply

    payload = dict(reply.payload)
    body = payload.get("body")
    if not isinstance(body, dict):
        return reply
    elements = body.get("elements")
    if not isinstance(elements, list):
        return reply

    enriched_body = dict(body)
    enriched_body["elements"] = [
        {"tag": "markdown", "content": block, "text_align": "left", "text_size": "normal"},
        *_remove_runtime_context_elements(elements),
    ]
    payload["body"] = enriched_body
    content = _append_runtime_context_text(reply.content, runtime_context)
    return ReplyPayload(
        channel=reply.channel,
        type=reply.type,
        content=content,
        payload=payload,
        intent=reply.intent,
        file_path=reply.file_path,
        metadata=dict(reply.metadata),
    )


def _load_project_runtime_context(workspace: PreparedWorkspace) -> dict[str, Any]:
    for path in (
        workspace.input_dir / "project_runtime_context.json",
        workspace.output_dir / "project_runtime_context.json",
        workspace.input_dir / "project_context.json",
    ):
        try:
            value = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if not isinstance(value, dict):
            continue
        if "project_runtime" in value and isinstance(value["project_runtime"], dict):
            return value["project_runtime"]
        if value.get("running_project"):
            return value
    return {}


def _runtime_context_markdown(runtime: dict[str, Any]) -> str:
    project = str(runtime.get("running_project") or runtime.get("business_project_name") or "").strip()
    project_path = str(runtime.get("project_path") or "").strip()
    version = str(
        runtime.get("normalized_version")
        or runtime.get("version_source_value")
        or runtime.get("execution_version")
        or runtime.get("current_version")
        or runtime.get("execution_describe")
        or runtime.get("current_describe")
        or ""
    ).strip()
    checkout = str(runtime.get("checkout_ref") or runtime.get("target_tag") or runtime.get("target_branch") or "").strip()
    target_branch = str(runtime.get("target_branch_ref") or runtime.get("target_branch") or "").strip()
    current_branch = str(runtime.get("current_branch") or "").strip()
    worktree = str(runtime.get("execution_path") or runtime.get("recommended_worktree") or "").strip()
    worktree_version = str(runtime.get("execution_version") or runtime.get("execution_describe") or "").strip()
    commit = str(runtime.get("execution_commit_sha") or runtime.get("current_commit_sha") or "").strip()
    describe = str(runtime.get("execution_describe") or runtime.get("current_describe") or "").strip()
    source = str(runtime.get("version_source_field") or "").strip()
    source_value = str(runtime.get("version_source_value") or "").strip()

    rows = []
    if project:
        rows.append(f"- 项目：{project}")
    if project_path:
        rows.append(f"- 项目目录：`{project_path}`")
    if version:
        suffix = f"（{source}: {source_value}）" if source and source_value and source_value != version else ""
        rows.append(f"- 版本：{version}{suffix}")
    if target_branch:
        rows.append(f"- 目标分支：{target_branch}")
    if current_branch and current_branch != target_branch:
        rows.append(f"- 主仓库分支：{current_branch}")
    if checkout:
        rows.append(f"- 检出：{checkout}")
    if worktree:
        rows.append(f"- Worktree：`{worktree}`")
    if worktree_version:
        rows.append(f"- Worktree 版本：{worktree_version}")
    if commit:
        commit_text = commit[:12]
        if describe:
            commit_text = f"{commit_text} / {describe}"
        rows.append(f"- 当前提交：{commit_text}")
    if not rows:
        return ""
    return "**运行上下文**\n" + "\n".join(rows)


def _remove_runtime_context_elements(elements: list[Any]) -> list[Any]:
    cleaned: list[Any] = []
    skip_next = False
    for item in elements:
        if skip_next:
            skip_next = False
            continue
        if not isinstance(item, dict):
            cleaned.append(item)
            continue

        content = str(item.get("content") or "")
        if item.get("tag") == "markdown" and _is_runtime_context_markdown(content):
            skip_next = content.strip() in {"**运行上下文**", "运行上下文"}
            continue
        cleaned.append(item)
    return cleaned


def _is_runtime_context_markdown(content: str) -> bool:
    text = str(content or "").strip()
    if not text:
        return False
    first_line = text.splitlines()[0].strip().replace("*", "")
    return first_line == "运行上下文"


def _append_runtime_context_text(content: str, runtime: dict[str, Any]) -> str:
    text = str(content or "").strip()
    block = _runtime_context_markdown(runtime).replace("**", "").replace("`", "")
    if not block or "运行上下文" in text:
        return text
    return f"{text}\n\n{block}" if text else block
