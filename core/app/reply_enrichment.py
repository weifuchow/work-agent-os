"""Reply enrichment based on prepared workspace context."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import re
import shutil
from typing import Any

from core.app.context import PreparedWorkspace
from core.app.project_workspace import active_project_entry, load_project_workspace
from core.ports import ReplyPayload


CODE_BLOCK_RE = re.compile(
    r"```(?P<info>[^\n`]*)\n(?P<code>.*?)```",
    re.DOTALL,
)

CARD_DETAIL_ATTACHMENT_KEY = "feishu_file_attachments"
CARD_DETAIL_THRESHOLD_CHARS = 1600
CARD_DETAIL_THRESHOLD_ELEMENTS = 10
CARD_DETAIL_PREVIEW_CHARS = 520
CARD_DETAIL_CODE_PREVIEW_CHARS = 260
CARD_DETAIL_MAX_ATTACHMENTS = 1
DECLARED_ATTACHMENT_KEYS = ("attachments", "files", "file_attachments")


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


def materialize_long_feishu_card_details(
    reply: ReplyPayload,
    workspace: PreparedWorkspace,
    *,
    message_id: int | str | None = None,
) -> ReplyPayload:
    if reply.type != "feishu_card" or not isinstance(reply.payload, dict):
        return reply
    if reply.metadata.get(CARD_DETAIL_ATTACHMENT_KEY):
        return reply

    payload = reply.payload
    body = payload.get("body")
    if not isinstance(body, dict):
        return reply
    elements = body.get("elements")
    if not isinstance(elements, list):
        return reply
    if not _should_materialize_card_details(elements):
        return reply

    title = _card_title(payload) or "完整分析"
    file_name = _detail_file_name(message_id)
    detail_path = _write_card_detail_markdown(
        workspace,
        file_name=file_name,
        title=title,
        payload=payload,
    )
    compact_payload = _compact_card_payload(payload, file_name=file_name)
    attachments = [
        {
            "path": str(detail_path.resolve()),
            "file_name": file_name,
            "file_type": "stream",
            "title": title,
            "description": "完整分析、证据明细、代码和长表格内容",
        }
    ][:CARD_DETAIL_MAX_ATTACHMENTS]

    return ReplyPayload(
        channel=reply.channel,
        type=reply.type,
        content=reply.content,
        payload=compact_payload,
        intent=reply.intent,
        file_path=reply.file_path,
        metadata={**dict(reply.metadata), CARD_DETAIL_ATTACHMENT_KEY: attachments},
    )


def attach_declared_feishu_reply_files(
    reply: ReplyPayload,
    workspace: PreparedWorkspace,
) -> ReplyPayload:
    declared = _declared_reply_attachments(reply, workspace)
    if not declared:
        return reply

    existing = [
        item
        for item in reply.metadata.get(CARD_DETAIL_ATTACHMENT_KEY, [])
        if isinstance(item, dict)
    ]
    merged = _merge_attachment_lists(existing, declared)
    payload = _append_declared_attachment_section(reply.payload, declared) if reply.type == "feishu_card" else reply.payload

    return ReplyPayload(
        channel=reply.channel,
        type=reply.type,
        content=reply.content,
        payload=payload,
        intent=reply.intent,
        file_path=reply.file_path,
        metadata={**dict(reply.metadata), CARD_DETAIL_ATTACHMENT_KEY: merged},
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


def _declared_reply_attachments(
    reply: ReplyPayload,
    workspace: PreparedWorkspace,
) -> list[dict[str, Any]]:
    raw_items: list[Any] = []
    if reply.file_path:
        raw_items.append({"path": reply.file_path})
    for key in DECLARED_ATTACHMENT_KEYS:
        value = reply.metadata.get(key)
        if isinstance(value, list):
            raw_items.extend(value)
        elif value:
            raw_items.append(value)
    if isinstance(reply.payload, dict):
        for key in DECLARED_ATTACHMENT_KEYS:
            value = reply.payload.get(key)
            if isinstance(value, list):
                raw_items.extend(value)
            elif value:
                raw_items.append(value)

    attachments: list[dict[str, Any]] = []
    for raw in raw_items:
        if isinstance(raw, dict) and reply.metadata.get("attachment_not_before_mtime"):
            raw = {**raw, "attachment_not_before_mtime": reply.metadata["attachment_not_before_mtime"]}
        item = _normalize_declared_attachment(raw, workspace)
        if item:
            attachments.append(item)
    return _merge_attachment_lists([], attachments)


def _normalize_declared_attachment(
    raw: Any,
    workspace: PreparedWorkspace,
) -> dict[str, Any] | None:
    if isinstance(raw, str):
        value = raw.strip()
        if not value:
            return None
        raw = {"path": value}
    if not isinstance(raw, dict):
        return None

    raw_path = str(
        raw.get("path")
        or raw.get("file_path")
        or raw.get("artifact_path")
        or raw.get("local_path")
        or ""
    ).strip()
    if not raw_path:
        return None
    path = _resolve_reply_attachment_path(raw_path, workspace)
    if not path or not path.is_file():
        return None
    if not _is_declared_attachment_current(path, raw):
        return None

    file_name = str(raw.get("file_name") or raw.get("name") or path.name).strip() or path.name
    path = _finalize_reply_attachment_path(path, workspace, file_name=file_name)
    title = str(raw.get("title") or raw.get("label") or _attachment_title_from_name(file_name)).strip()
    description = str(raw.get("description") or raw.get("summary") or "").strip()
    file_type = str(raw.get("file_type") or _feishu_file_type_for_path(path)).strip() or "stream"
    url = str(raw.get("url") or raw.get("href") or raw.get("link") or "").strip()

    normalized = {
        "path": str(path.resolve()),
        "file_name": file_name,
        "file_type": file_type,
        "title": title,
        "description": description,
    }
    if url:
        normalized["url"] = url
    return normalized


def _is_declared_attachment_current(path: Path, raw: dict[str, Any]) -> bool:
    if str(raw.get("url") or raw.get("href") or raw.get("link") or "").strip():
        return True
    allow_stale = raw.get("allow_stale") if "allow_stale" in raw else raw.get("allow_existing")
    if _bool_value(allow_stale):
        return True
    not_before = _float_value(raw.get("attachment_not_before_mtime") or raw.get("not_before_mtime"))
    if not not_before:
        return True
    try:
        return path.stat().st_mtime + 2.0 >= not_before
    except OSError:
        return False


def _finalize_reply_attachment_path(path: Path, workspace: PreparedWorkspace, *, file_name: str) -> Path:
    try:
        resolved = path.resolve()
        output_dir = workspace.output_dir.resolve()
        resolved.relative_to(output_dir)
        return resolved
    except (OSError, ValueError):
        pass

    workspace.output_dir.mkdir(parents=True, exist_ok=True)
    target = _unique_output_attachment_path(workspace.output_dir, file_name=file_name, source_path=path)
    try:
        if path.resolve() != target.resolve():
            shutil.copy2(path, target)
        return target
    except OSError:
        return path


def _unique_output_attachment_path(output_dir: Path, *, file_name: str, source_path: Path) -> Path:
    safe_name = Path(file_name).name or source_path.name
    target = output_dir / safe_name
    if not target.exists():
        return target
    try:
        if target.resolve() == source_path.resolve():
            return target
    except OSError:
        pass
    stem = Path(safe_name).stem or "attachment"
    suffix = Path(safe_name).suffix
    digest = hashlib.sha1(str(source_path.resolve()).encode("utf-8", errors="ignore")).hexdigest()[:8]
    return output_dir / f"{stem}-{digest}{suffix}"


def _bool_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _float_value(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _resolve_reply_attachment_path(raw_path: str, workspace: PreparedWorkspace) -> Path | None:
    path = Path(raw_path)
    candidates = [path] if path.is_absolute() else [
        workspace.path / path,
        workspace.output_dir / path,
        workspace.artifacts_dir / path,
        Path(workspace.artifact_roots.get("session_dir") or "") / path,
    ]
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved.is_file():
            return resolved
    return None


def _merge_attachment_lists(
    existing: list[dict[str, Any]],
    extra: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in [*existing, *extra]:
        path = str(item.get("path") or "").strip()
        if not path or path in seen:
            continue
        seen.add(path)
        merged.append(dict(item))
    return merged


def _append_declared_attachment_section(payload: Any, attachments: list[dict[str, Any]]) -> Any:
    if not isinstance(payload, dict):
        return payload
    body = payload.get("body")
    if not isinstance(body, dict):
        return payload
    elements = body.get("elements")
    if not isinstance(elements, list):
        return payload

    block = _declared_attachment_markdown(attachments)
    if not block:
        return payload
    compact = copy.deepcopy(payload)
    compact_body = dict(compact.get("body") or {})
    compact_body["elements"] = [
        *_remove_declared_attachment_elements(compact_body.get("elements") or []),
        {"tag": "markdown", "content": block, "text_align": "left", "text_size": "normal"},
    ]
    compact["body"] = compact_body
    return compact


def _remove_declared_attachment_elements(elements: list[Any]) -> list[Any]:
    cleaned: list[Any] = []
    for item in elements:
        if (
            isinstance(item, dict)
            and item.get("tag") == "markdown"
            and _is_declared_attachment_markdown(str(item.get("content") or ""))
        ):
            continue
        cleaned.append(item)
    return cleaned


def _declared_attachment_markdown(attachments: list[dict[str, Any]]) -> str:
    lines = ["**附件**", "已随本话题发送；点击下方文件卡片打开。"]
    for item in attachments:
        file_name = str(item.get("file_name") or Path(str(item.get("path") or "")).name).strip()
        title = str(item.get("title") or _attachment_title_from_name(file_name)).strip()
        description = str(item.get("description") or "").strip()
        url = str(item.get("url") or "").strip()
        label = f"[{title}]({url})" if url else title
        meta = _attachment_short_meta(file_name, description)
        lines.append(f"- {label}{meta}")
    return "\n".join(lines) if len(lines) > 1 else ""


def _attachment_short_meta(file_name: str, description: str) -> str:
    parts: list[str] = []
    suffix = Path(file_name).suffix.lower().lstrip(".")
    if suffix:
        parts.append(suffix.upper())
    compact_description = _truncate_inline(description, 36)
    if compact_description:
        parts.append(compact_description)
    return f"（{'，'.join(parts)}）" if parts else ""


def _truncate_inline(value: str, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _is_declared_attachment_markdown(content: str) -> bool:
    first_line = str(content or "").strip().splitlines()[0].strip().replace("*", "") if str(content or "").strip() else ""
    return first_line == "附件"


def _attachment_title_from_name(file_name: str) -> str:
    suffix = Path(file_name).suffix.lower()
    if suffix == ".pdf":
        return "PDF 文件"
    if suffix in {".doc", ".docx"}:
        return "Word 文件"
    if suffix in {".md", ".markdown"}:
        return "Markdown 文件"
    return "附件文件"


def _feishu_file_type_for_path(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".doc", ".docx", ".pdf", ".md", ".markdown", ".txt"}:
        return "stream"
    if suffix in {".mp4", ".mov", ".avi"}:
        return "mp4"
    if suffix in {".mp3", ".wav", ".m4a"}:
        return "opus"
    return "stream"


def _should_materialize_card_details(elements: list[Any]) -> bool:
    text_chars = 0
    markdown_count = 0
    for item in elements:
        if not isinstance(item, dict):
            continue
        tag = str(item.get("tag") or "").strip()
        if tag == "markdown":
            content = str(item.get("content") or "")
            markdown_count += 1
            text_chars += len(content)
            if _contains_non_mermaid_code(content):
                return True
        elif tag == "table":
            rows = item.get("rows")
            if isinstance(rows, list) and len(rows) > 6:
                return True
            text_chars += len(json.dumps(item, ensure_ascii=False))
        else:
            text_chars += len(json.dumps(item, ensure_ascii=False, default=str))
    return text_chars > CARD_DETAIL_THRESHOLD_CHARS or markdown_count > CARD_DETAIL_THRESHOLD_ELEMENTS


def _write_card_detail_markdown(
    workspace: PreparedWorkspace,
    *,
    file_name: str,
    title: str,
    payload: dict[str, Any],
) -> Path:
    target_dir = workspace.output_dir / "reply_artifacts"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / file_name
    target.write_text(_card_payload_to_markdown(payload, title=title), encoding="utf-8")
    return target


def _compact_card_payload(payload: dict[str, Any], *, file_name: str) -> dict[str, Any]:
    compact = copy.deepcopy(payload)
    body = compact.get("body")
    if not isinstance(body, dict):
        return compact
    elements = body.get("elements")
    if not isinstance(elements, list):
        return compact

    compact_elements = _compact_card_elements(elements)
    compact_elements.append(
        {
            "tag": "markdown",
            "content": (
                "**完整细节**\n"
                f"已作为飞书 Markdown 附件发送：`{file_name}`。"
                "卡片保留适合快速阅读的内容；代码、设计文档、分析过程和长证据明细在附件中查看。"
            ),
            "text_align": "left",
            "text_size": "normal",
        }
    )
    compact_body = dict(body)
    compact_body["elements"] = compact_elements
    compact["body"] = compact_body
    return compact


def _compact_card_elements(elements: list[Any]) -> list[Any]:
    compacted: list[Any] = []
    for item in elements:
        if not isinstance(item, dict):
            continue
        tag = str(item.get("tag") or "").strip()
        if tag == "markdown":
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            if _contains_mermaid_code(content):
                compacted.append(copy.deepcopy(item))
                continue
            if _contains_non_mermaid_code(content):
                compacted.extend(_code_summary_elements(item))
                continue
            item = copy.deepcopy(item)
            item["content"] = _truncate_markdown(content, CARD_DETAIL_PREVIEW_CHARS)
            compacted.append(item)
            continue
        item = copy.deepcopy(item)
        if tag == "table":
            item = _compact_element(item)
        compacted.append(item)
    return compacted


def _compact_element(item: dict[str, Any]) -> dict[str, Any]:
    if item.get("tag") == "markdown":
        content = str(item.get("content") or "")
        if not _contains_mermaid_code(content):
            item["content"] = _truncate_markdown(content, CARD_DETAIL_PREVIEW_CHARS)
        return item
    if item.get("tag") == "table":
        rows = item.get("rows")
        if isinstance(rows, list) and len(rows) > 6:
            item["rows"] = rows[:6]
            item["page_size"] = min(int(item.get("page_size") or 6), 6)
        return item
    return item


def _code_summary_elements(source_element: dict[str, Any]) -> list[dict[str, Any]]:
    content = str(source_element.get("content") or "")
    result: list[dict[str, Any]] = []
    cursor = 0
    for match in CODE_BLOCK_RE.finditer(content):
        before = content[cursor:match.start()].strip()
        if before:
            result.append(_markdown_like(source_element, _truncate_markdown(before, CARD_DETAIL_CODE_PREVIEW_CHARS)))

        info = match.group("info").strip()
        code = match.group("code").strip()
        language, path = _parse_code_info(info)
        line_count = len(code.splitlines()) if code else 0
        title = path or language or "代码片段"
        details = [f"**代码片段：{title}**"]
        meta: list[str] = []
        if path:
            meta.append(f"文件 `{path}`")
        if language:
            meta.append(f"语言 `{language}`")
        if line_count:
            meta.append(f"{line_count} 行")
        if meta:
            details.append(" · ".join(meta))
        details.append("完整代码见飞书 Markdown 附件。")
        result.append(_markdown_like(source_element, "\n".join(details)))
        cursor = match.end()

    tail = content[cursor:].strip()
    if tail:
        result.append(_markdown_like(source_element, _truncate_markdown(tail, CARD_DETAIL_CODE_PREVIEW_CHARS)))
    return result


def _card_payload_to_markdown(payload: dict[str, Any], *, title: str) -> str:
    lines = [f"# {title}", ""]
    body = payload.get("body")
    elements = body.get("elements") if isinstance(body, dict) else []
    for block in _elements_to_markdown(elements):
        if block:
            lines.extend([block, ""])
    return "\n".join(lines).strip() + "\n"


def _elements_to_markdown(value: Any) -> list[str]:
    if isinstance(value, list):
        blocks: list[str] = []
        for item in value:
            blocks.extend(_elements_to_markdown(item))
        return blocks
    if not isinstance(value, dict):
        return []

    tag = str(value.get("tag") or "").strip()
    if tag == "markdown":
        content = str(value.get("content") or "").strip()
        return [content] if content else []
    if tag in {"plain_text", "lark_md"}:
        content = str(value.get("content") or value.get("text") or "").strip()
        return [content] if content else []
    if tag == "img":
        alt = value.get("alt") if isinstance(value.get("alt"), dict) else {}
        title = str(alt.get("content") or "图片").strip()
        image_key = str(value.get("img_key") or "").strip()
        suffix = f" `{image_key}`" if image_key else ""
        return [f"![{title}]{suffix}"]
    if tag == "table":
        rendered = _table_to_markdown(value)
        return [rendered] if rendered else []

    nested_blocks: list[str] = []
    text_obj = value.get("text")
    if isinstance(text_obj, dict):
        content = str(text_obj.get("content") or "").strip()
        if content:
            nested_blocks.append(content)
    for key in ("elements", "columns"):
        nested = value.get(key)
        if isinstance(nested, list):
            nested_blocks.extend(_elements_to_markdown(nested))
    return nested_blocks


def _table_to_markdown(table: dict[str, Any]) -> str:
    rows = [row for row in table.get("rows") or [] if isinstance(row, dict)]
    columns = table.get("columns")
    if not isinstance(columns, list) or not columns:
        if not rows:
            return ""
        columns = list(rows[0].keys())

    keys: list[str] = []
    headers: list[str] = []
    for index, column in enumerate(columns):
        if isinstance(column, dict):
            key = str(column.get("name") or column.get("key") or f"col_{index + 1}")
            header = str(column.get("display_name") or column.get("label") or key)
        else:
            key = str(column or f"col_{index + 1}")
            header = key
        keys.append(key)
        headers.append(header)
    if not keys:
        return ""

    lines = [
        "| " + " | ".join(_escape_table_cell(item) for item in headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(_escape_table_cell(row.get(key, "")) for key in keys) + " |")
    return "\n".join(lines)


def _card_title(payload: dict[str, Any]) -> str:
    header = payload.get("header")
    if not isinstance(header, dict):
        return ""
    title = header.get("title")
    if isinstance(title, dict):
        return str(title.get("content") or "").strip()
    return ""


def _detail_file_name(message_id: int | str | None) -> str:
    suffix = str(message_id or "").strip()
    if suffix:
        suffix = re.sub(r"[^A-Za-z0-9_.-]+", "-", suffix).strip("-._")
    return f"reply-details-{suffix or 'latest'}.md"


def _contains_mermaid_code(content: str) -> bool:
    for match in CODE_BLOCK_RE.finditer(str(content or "")):
        if match.group("info").strip().lower().startswith("mermaid"):
            return True
    return False


def _contains_non_mermaid_code(content: str) -> bool:
    for match in CODE_BLOCK_RE.finditer(str(content or "")):
        if not match.group("info").strip().lower().startswith("mermaid"):
            return True
    return False


def _truncate_markdown(content: str, limit: int) -> str:
    text = str(content or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n\n…完整内容见飞书附件。"


def _escape_table_cell(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", "<br>")
