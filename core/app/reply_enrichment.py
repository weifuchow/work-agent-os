"""Reply enrichment based on prepared workspace context."""

from __future__ import annotations

import re
from typing import Any

from core.app.context import PreparedWorkspace
from core.app.project_workspace import active_project_entry, load_project_workspace
from core.ports import ReplyPayload


CODE_BLOCK_RE = re.compile(
    r"```(?P<info>[^\n`]*)\n(?P<code>.*?)```",
    re.DOTALL,
)


def enhance_feishu_card_code_blocks(reply: ReplyPayload) -> ReplyPayload:
    if reply.type != "feishu_card" or not isinstance(reply.payload, dict):
        return reply

    payload = dict(reply.payload)
    body = payload.get("body")
    if not isinstance(body, dict):
        return reply
    elements = body.get("elements")
    if not isinstance(elements, list):
        return reply

    enhanced = _enhance_code_block_elements(elements)
    if enhanced == elements:
        return reply

    enriched_body = dict(body)
    enriched_body["elements"] = enhanced
    payload["body"] = enriched_body
    return ReplyPayload(
        channel=reply.channel,
        type=reply.type,
        content=reply.content,
        payload=payload,
        intent=reply.intent,
        file_path=reply.file_path,
        metadata=dict(reply.metadata),
    )


def enrich_reply_with_workspace_context(
    reply: ReplyPayload,
    workspace: PreparedWorkspace,
) -> ReplyPayload:
    project_workspace = load_project_workspace(workspace)
    if not project_workspace:
        return reply
    if reply.type != "feishu_card" or not isinstance(reply.payload, dict):
        return reply

    block = _project_workspace_markdown(project_workspace)
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
    content = _append_runtime_context_text(reply.content, project_workspace)
    return ReplyPayload(
        channel=reply.channel,
        type=reply.type,
        content=content,
        payload=payload,
        intent=reply.intent,
        file_path=reply.file_path,
        metadata=dict(reply.metadata),
    )


def _project_workspace_markdown(project_workspace: dict[str, Any]) -> str:
    projects = project_workspace.get("projects") if isinstance(project_workspace, dict) else {}
    if not isinstance(projects, dict) or not projects:
        return ""
    active = str(project_workspace.get("active_project") or "").strip()
    ordered_names = [
        name
        for name in project_workspace.get("project_order", [])
        if isinstance(name, str) and isinstance(projects.get(name), dict)
    ]
    for name in projects:
        if name not in ordered_names and isinstance(projects.get(name), dict):
            ordered_names.append(name)
    if active in ordered_names:
        ordered_names.remove(active)
        ordered_names.insert(0, active)

    active_block = ""
    related_blocks: list[str] = []
    for index, name in enumerate(ordered_names):
        runtime = projects[name]
        if index == 0:
            active_block = _active_runtime_context_markdown(runtime)
        else:
            block = _related_runtime_context_markdown(runtime)
            if block:
                related_blocks.append(block)
    if not active_block:
        active_entry = active_project_entry(project_workspace)
        active_block = _active_runtime_context_markdown(active_entry)
    if not active_block and not related_blocks:
        return ""
    blocks = ["**运行上下文**"]
    if active_block:
        blocks.append(active_block)
    if related_blocks:
        blocks.append("**相关项目**\n" + "\n".join(related_blocks))
    return "\n\n".join(blocks)


def _active_runtime_context_markdown(runtime: dict[str, Any]) -> str:
    fields = _runtime_fields(runtime)
    project = fields["project"]
    if not project:
        return ""

    details = []
    if fields["version"]:
        details.append(f"版本 `{fields['version']}`")
    if fields["checkout"]:
        details.append(f"检出 `{fields['checkout']}`")
    if fields["current_branch"] and fields["current_branch"] != fields["target_branch"]:
        details.append(f"主仓库 `{fields['current_branch']}`")

    lines = [f"**当前项目：{project}**"]
    if details:
        lines.append(" · ".join(details))
    if fields["source_path"]:
        lines.append(f"源仓库：`{_compact_path(fields['source_path'])}`")
    if fields["worktree"]:
        lines.append(f"Worktree：`{_compact_path(fields['worktree'])}`")
    if fields["commit_text"]:
        lines.append(f"提交：`{fields['commit_text']}`")
    return "\n".join(lines)


def _related_runtime_context_markdown(runtime: dict[str, Any]) -> str:
    fields = _runtime_fields(runtime)
    project = fields["project"]
    if not project:
        return ""
    details = []
    if fields["version"]:
        details.append(f"版本 `{fields['version']}`")
    if fields["checkout"]:
        details.append(f"检出 `{fields['checkout']}`")
    if fields["worktree"]:
        details.append(f"Worktree `{_compact_path(fields['worktree'])}`")
    suffix = "，".join(details)
    return f"- {project}：{suffix}" if suffix else f"- {project}"


def _runtime_fields(runtime: dict[str, Any]) -> dict[str, str]:
    project = str(runtime.get("running_project") or runtime.get("business_project_name") or "").strip()
    project_path = str(runtime.get("source_path") or runtime.get("project_path") or "").strip()
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
    worktree = str(runtime.get("worktree_path") or runtime.get("execution_path") or runtime.get("recommended_worktree") or "").strip()
    worktree_version = str(runtime.get("execution_version") or runtime.get("execution_describe") or "").strip()
    commit = str(runtime.get("execution_commit_sha") or runtime.get("current_commit_sha") or "").strip()
    describe = str(runtime.get("execution_describe") or runtime.get("current_describe") or "").strip()
    source = str(runtime.get("version_source_field") or "").strip()
    source_value = str(runtime.get("version_source_value") or "").strip()

    if version and source and source_value and source_value != version:
        version = f"{version}（{source}: {source_value}）"

    commit_text = ""
    if commit:
        commit_text = commit[:12]
        if describe:
            commit_text = f"{commit_text} / {describe}"
    elif worktree_version:
        commit_text = worktree_version

    return {
        "project": project,
        "source_path": project_path,
        "version": version,
        "target_branch": target_branch,
        "current_branch": current_branch,
        "checkout": checkout,
        "worktree": worktree,
        "commit_text": commit_text,
    }


def _compact_path(value: str, *, keep_parts: int = 4) -> str:
    path = str(value or "").strip()
    if not path:
        return ""
    normalized = path.replace("/", "\\")
    parts = [part for part in normalized.split("\\") if part]
    if len(parts) <= keep_parts + 1:
        return path
    prefix = parts[0] if parts[0].endswith(":") else ""
    suffix = "\\".join(parts[-keep_parts:])
    return f"{prefix}\\...\\{suffix}" if prefix else f"...\\{suffix}"


def _enhance_code_block_elements(elements: list[Any]) -> list[Any]:
    enhanced: list[Any] = []
    changed = False
    for item in elements:
        if not isinstance(item, dict) or item.get("tag") != "markdown":
            enhanced.append(item)
            continue

        content = str(item.get("content") or "")
        if "```" not in content:
            enhanced.append(item)
            continue
        if _is_generated_code_body(content, enhanced):
            enhanced.append(item)
            continue

        split = _split_markdown_code_blocks(item)
        if len(split) == 1 and split[0] == item:
            enhanced.append(item)
            continue
        enhanced.extend(split)
        changed = True
    return enhanced if changed else elements


def _is_generated_code_body(content: str, previous_elements: list[Any]) -> bool:
    if not content.strip().startswith("```"):
        return False
    if not previous_elements:
        return False
    previous = previous_elements[-1]
    if not isinstance(previous, dict) or previous.get("tag") != "markdown":
        return False
    return "代码片段" in str(previous.get("content") or "")


def _split_markdown_code_blocks(element: dict[str, Any]) -> list[dict[str, Any]]:
    content = str(element.get("content") or "")
    result: list[dict[str, Any]] = []
    cursor = 0
    changed = False
    for match in CODE_BLOCK_RE.finditer(content):
        before = content[cursor:match.start()].strip()
        if before:
            result.append(_markdown_like(element, before))

        info = match.group("info").strip()
        code = match.group("code").strip()
        language, path = _parse_code_info(info)
        if language.lower() == "mermaid":
            result.append(_markdown_like(element, match.group(0).strip()))
        elif code:
            result.extend(_code_panel_elements(element, code=code, language=language, path=path))
            changed = True
        cursor = match.end()

    tail = content[cursor:].strip()
    if tail:
        result.append(_markdown_like(element, tail))
    if not changed:
        return [element]
    return result


def _code_panel_elements(
    source_element: dict[str, Any],
    *,
    code: str,
    language: str,
    path: str,
) -> list[dict[str, Any]]:
    title = path or language or "代码片段"
    meta = [f"**代码片段：{title}**"]
    details = []
    if path:
        details.append(f"文件 `{path}`")
    if language:
        details.append(f"语言 `{language}`")
    if details:
        meta.append(" · ".join(details))
    return [
        _markdown_like(source_element, "\n".join(meta)),
        _markdown_like(source_element, _render_code_fence(code, language)),
    ]


def _parse_code_info(info: str) -> tuple[str, str]:
    raw = str(info or "").strip()
    if not raw:
        return "", ""
    parts = raw.split()
    language = parts[0].strip() if parts else ""
    path = ""
    for part in parts[1:]:
        if part.startswith(("path=", "file=")):
            path = part.split("=", 1)[1].strip().strip("'\"")
    if not path and len(parts) >= 2 and any(token in parts[1] for token in ("/", "\\", ".")):
        path = parts[1].strip().strip("'\"")
    return language, path


def _render_code_fence(code: str, language: str = "") -> str:
    lang = re.sub(r"[^A-Za-z0-9_+.#-]+", "", str(language or "").strip())
    return f"```{lang}\n{str(code).strip()}\n```"


def _markdown_like(source_element: dict[str, Any], content: str) -> dict[str, Any]:
    return {
        "tag": "markdown",
        "content": content,
        "text_align": str(source_element.get("text_align") or "left"),
        "text_size": str(source_element.get("text_size") or "normal"),
    }


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


def _append_runtime_context_text(content: str, project_workspace: dict[str, Any]) -> str:
    text = str(content or "").strip()
    block = _project_workspace_markdown(project_workspace).replace("**", "").replace("`", "")
    if not block or "运行上下文" in text:
        return text
    return f"{text}\n\n{block}" if text else block
