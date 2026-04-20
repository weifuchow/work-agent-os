"""Message processing pipeline.

Two-level agent architecture:
- Main Agent: classifies messages, dispatches to project agents
- Project Agent: executes in project directory with full context

Pipeline handles ALL IO (feishu reply, DB writes). Agent only outputs JSON.
All DB access uses raw SQL (aiosqlite) to avoid ORM cache issues.
"""

import asyncio
from collections.abc import AsyncIterable
import base64
import hashlib
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib import request as urlrequest

import aiosqlite
from loguru import logger

from core.config import (
    get_agent_runtime_override,
    get_default_model_for_runtime,
    get_model_override,
    load_models_config,
    settings,
)
from core.orchestrator.agent_runtime import (
    DEFAULT_AGENT_RUNTIME,
    get_agent_run_runtime_type,
    normalize_agent_runtime,
)

DB_PATH = str(Path(settings.db_dir) / "app.sqlite")


# ---------------------------------------------------------------------------
# Per-session lock registry — serialize messages within the same session
# ---------------------------------------------------------------------------

_session_locks: dict[int, asyncio.Lock] = {}
_session_locks_guard = asyncio.Lock()


async def _get_session_lock(session_id: int) -> asyncio.Lock:
    async with _session_locks_guard:
        if session_id not in _session_locks:
            _session_locks[session_id] = asyncio.Lock()
        return _session_locks[session_id]


# ---------------------------------------------------------------------------
# System Prompt (template — projects injected at runtime)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT_TEMPLATE = """你是用户的私人工作助理，通过飞书接收消息。

## 已注册项目
{projects_section}

## 核心规则

1. **项目匹配优先**：如果消息涉及上方任何项目（按名称/别名/关键词匹配），你**必须调用 `dispatch_to_project` tool**，将 dispatch 返回的结果作为 reply_content。**绝对不能跳过 dispatch 用项目描述自行回答。**
2. **所有消息必须回复**：无论是闲聊、问候、表情、系统通知，还是工作问题，都必须生成 reply_content 进行回复。
3. 高风险操作（承诺排期/确认上线/技术拍板）使用 action=drafted。

## 输出格式

最终输出必须是合法 JSON：
```json
{{
  "action": "replied | drafted",
  "classified_type": "chat | work_question | urgent_issue | task_request | noise",
  "topic": "主题",
  "project_name": "项目名或null",
  "reply_content": "回复内容（必填，可为字符串或结构化对象）",
  "reason": "处理理由"
}}
```
- `reply_content` 必填，系统用它发送飞书消息
- 默认情况下，`reply_content` 直接写字符串，系统会按普通文本发送
- 如果是工作类问题，优先输出结构化对象而不是纯字符串。普通分析/说明建议用 `format=rich`，建议格式：
```json
{{
  "format": "rich",
  "title": "标题",
  "summary": "一句话总结",
  "sections": [
    {{"title": "结论", "content": "核心结论"}},
    {{"title": "说明", "content": "补充说明"}}
  ],
  "table": {{
    "columns": [
      {{"key": "item", "label": "检查项", "type": "text"}},
      {{"key": "status", "label": "状态", "type": "text"}}
    ],
    "rows": [
      {{"item": "配置", "status": "OK"}}
    ]
  }},
  "fallback_text": "纯文本兜底内容"
}}
```
- 如果用户询问流程、步骤、排查路径、发布链路、审批流、时序关系，优先输出结构化对象，建议格式：
```json
{{
  "format": "flow",
  "title": "流程标题",
  "summary": "一句话说明",
  "steps": [
    {{"title": "步骤1", "detail": "说明"}},
    {{"title": "步骤2", "detail": "说明"}}
  ],
  "table": {{
    "columns": [
      {{"key": "step", "label": "步骤", "type": "text"}},
      {{"key": "owner", "label": "负责人", "type": "text"}},
      {{"key": "output", "label": "产出", "type": "markdown"}}
    ],
    "rows": [
      {{"step": "准备", "owner": "后端", "output": "检查配置"}}
    ]
  }},
  "mermaid": "flowchart TD\\nA[开始] --> B[结束]",
  "fallback_text": "纯文本兜底内容"
}}
```
- `format=rich` / `format=flow` 时，系统会优先渲染为飞书卡片；有 `table` 时会渲染表格；有 `mermaid` 时会尝试转成图片嵌入卡片
- 飞书不保证原生渲染 Mermaid，所以不要把 Mermaid 代码直接发给用户；有 `mermaid` 时请同时给出 `steps` 和 `fallback_text`
"""


def _build_system_prompt() -> str:
    from core.projects import get_projects
    projects = get_projects()
    lines = []
    for p in projects:
        desc = p.description.replace("\n", " ").strip()
        lines.append(f"- **{p.name}**: {desc}")
    section = "\n".join(lines) if lines else "（暂无注册项目）"
    return SYSTEM_PROMPT_TEMPLATE.format(projects_section=section)


def _safe_json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _looks_like_structured_reply(value: Any) -> bool:
    if not isinstance(value, dict):
        return False

    if value.get("format") in {"flow", "rich"}:
        return True

    if value.get("msg_type") in {"text", "post", "interactive", "image"}:
        return True

    semantic_keys = {"title", "summary", "steps", "sections", "table", "mermaid", "fallback_text"}
    return any(key in value for key in semantic_keys)


def _extract_structured_reply_payload(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if not text:
        return None

    def _pick(value: Any) -> dict[str, Any] | None:
        if _looks_like_structured_reply(value):
            return value
        if isinstance(value, dict):
            reply_content = value.get("reply_content")
            if _looks_like_structured_reply(reply_content):
                return reply_content
        return None

    candidates = [text]
    if "```" in text:
        for block in text.split("```"):
            block = block.strip()
            if block.startswith("json"):
                block = block[4:].strip()
            if block:
                candidates.append(block)

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        picked = _pick(parsed)
        if picked:
            return picked

    return None


def _reply_content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if content is None:
        return ""

    if isinstance(content, dict):
        fallback = content.get("fallback_text")
        if isinstance(fallback, str) and fallback.strip():
            return fallback.strip()

        raw_content = content.get("content")
        if content.get("msg_type") == "text":
            if isinstance(raw_content, dict):
                return str(raw_content.get("text", "") or "")
            return str(raw_content or "")

        parts: list[str] = []
        title = str(content.get("title", "") or "").strip()
        summary = str(content.get("summary", "") or "").strip()
        if title:
            parts.append(title)
        if summary:
            parts.append(summary)

        steps = content.get("steps") or []
        if isinstance(steps, list):
            rendered_steps: list[str] = []
            for idx, step in enumerate(steps, start=1):
                if isinstance(step, dict):
                    step_title = str(step.get("title", "") or f"步骤{idx}").strip()
                    detail = str(step.get("detail", "") or "").strip()
                    rendered = f"{idx}. {step_title}"
                    if detail:
                        rendered += f" - {detail}"
                    rendered_steps.append(rendered)
                elif step:
                    rendered_steps.append(f"{idx}. {step}")
            if rendered_steps:
                parts.append("\n".join(rendered_steps))

        if parts:
            return "\n\n".join(parts)

    return str(content)


_INTERNAL_TOOL_ERROR_MARKERS = (
    "user cancelled mcp tool call",
    "cancelled mcp tool call",
    "mcp tool call cancelled",
    "tool call cancelled",
    "tool call failed",
    "mcp error",
)


def _contains_internal_tool_error(content: Any) -> bool:
    text = _reply_content_to_text(content).strip()
    if not text:
        return False
    lowered = re.sub(r"\s+", " ", text.lower())
    return any(marker in lowered for marker in _INTERNAL_TOOL_ERROR_MARKERS)


def _build_internal_tool_error_reply(
    *,
    classified_type: str | None,
    project_name: str = "",
) -> str:
    project_hint = f"`{project_name}` 项目" if project_name else "项目"
    if classified_type in {"work_question", "urgent_issue", "task_request"}:
        return (
            f"{project_hint} 的内部调用刚才被中断了，我这边没有拿到有效结论。"
            "请先等上一条消息处理完成后再继续发送；如果还需要，我可以重新处理一次。"
        )
    return "刚才的内部调用被中断了，请稍后重试。"


def _should_cardify_text_reply(text: str, classified_type: str | None) -> bool:
    if classified_type not in {"work_question", "urgent_issue", "task_request"}:
        return False

    stripped = (text or "").strip()
    return bool(stripped)


def _infer_reply_reasoning_level(
    model_id: str | None,
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_model = str(model_id or "").strip().lower()
    usage = usage if isinstance(usage, dict) else {}

    try:
        reasoning_tokens = int(usage.get("reasoning_output_tokens") or 0)
    except (TypeError, ValueError):
        reasoning_tokens = 0

    if reasoning_tokens >= 1800:
        level = "high"
    elif reasoning_tokens >= 400:
        level = "medium"
    elif any(marker in normalized_model for marker in ("haiku", "mini", "flash", "nano")):
        level = "low"
    elif any(marker in normalized_model for marker in ("opus", "gpt-5.4", "o3", "o4")):
        level = "high"
    elif normalized_model:
        level = "medium"
    else:
        level = "medium"

    return {
        "model": model_id or "",
        "reasoning_level": level,
        "reasoning_output_tokens": reasoning_tokens,
    }


def _select_reply_presentation(
    content: Any,
    *,
    classified_type: str | None,
    model_id: str | None,
    usage: dict[str, Any] | None,
) -> dict[str, Any]:
    if isinstance(content, dict):
        explicit = str(
            content.get("reply_style")
            or content.get("card_variant")
            or content.get("presentation")
            or ""
        ).strip()
        if explicit:
            reasoning = _infer_reply_reasoning_level(model_id, usage)
            return {
                "variant": explicit,
                "model": reasoning["model"],
                "reasoning_level": reasoning["reasoning_level"],
                "reasoning_output_tokens": reasoning["reasoning_output_tokens"],
            }

    reasoning = _infer_reply_reasoning_level(model_id, usage)
    variant = "plain"
    if classified_type in {"work_question", "urgent_issue", "task_request"}:
        if reasoning["reasoning_level"] == "low":
            variant = "plain"
        elif reasoning["reasoning_level"] == "high":
            variant = "deep"
        else:
            variant = "standard"
    return {
        "variant": variant,
        "model": reasoning["model"],
        "reasoning_level": reasoning["reasoning_level"],
        "reasoning_output_tokens": reasoning["reasoning_output_tokens"],
    }


def _normalize_flow_table(table: dict[str, Any]) -> dict[str, Any] | None:
    rows = [row for row in (table.get("rows") or []) if isinstance(row, dict)]
    raw_columns = table.get("columns") or []

    if not raw_columns and rows:
        raw_columns = [{"key": key, "label": key, "type": "text"} for key in rows[0].keys()]

    allowed_types = {"text", "lark_md", "options", "number", "persons", "date", "markdown"}
    columns: list[dict[str, Any]] = []
    keys: list[str] = []

    for index, column in enumerate(raw_columns):
        if isinstance(column, str):
            key = column.strip() or f"col_{index + 1}"
            display_name = key
            data_type = "text"
            width = None
            horizontal_align = None
            vertical_align = None
            date_format = None
            number_format = None
        elif isinstance(column, dict):
            key = str(
                column.get("key")
                or column.get("name")
                or column.get("field")
                or column.get("label")
                or f"col_{index + 1}"
            ).strip()
            display_name = str(
                column.get("label")
                or column.get("display_name")
                or column.get("title")
                or key
            ).strip()
            data_type = str(column.get("type") or column.get("data_type") or "text").strip()
            width = column.get("width")
            horizontal_align = column.get("horizontal_align")
            vertical_align = column.get("vertical_align")
            date_format = column.get("date_format")
            number_format = column.get("format")
        else:
            continue

        if not key:
            continue
        if data_type not in allowed_types:
            data_type = "text"

        normalized_column = {
            "name": key,
            "display_name": display_name or key,
            "data_type": data_type,
        }
        if width:
            normalized_column["width"] = width
        if horizontal_align:
            normalized_column["horizontal_align"] = horizontal_align
        if vertical_align:
            normalized_column["vertical_align"] = vertical_align
        if date_format and data_type == "date":
            normalized_column["date_format"] = date_format
        if isinstance(number_format, dict) and data_type == "number":
            normalized_column["format"] = number_format

        columns.append(normalized_column)
        keys.append(key)

    if not columns:
        return None

    normalized_rows = [{key: row.get(key, "") for key in keys} for row in rows]
    return {
        "tag": "table",
        "page_size": min(max(len(normalized_rows), 1), 10),
        "row_height": "middle",
        "header_style": {
            "text_align": "left",
            "text_size": "normal",
            "background_style": "grey",
            "text_color": "default",
            "bold": True,
            "lines": 1,
        },
        "columns": columns,
        "rows": normalized_rows,
    }


def _markdown_element(content: str, *, text_size: str = "normal", margin: str = "0px 0px 0px 0px") -> dict[str, Any]:
    return {
        "tag": "markdown",
        "content": content,
        "text_align": "left",
        "text_size": text_size,
        "margin": margin,
    }


def _plain_text_div(
    content: str,
    *,
    text_size: str = "normal",
    text_color: str = "default",
    margin: str = "0px 0px 0px 0px",
) -> dict[str, Any]:
    return {
        "tag": "div",
        "text": {
            "tag": "plain_text",
            "content": content,
            "text_size": text_size,
            "text_align": "left",
            "text_color": text_color,
        },
        "margin": margin,
    }


def _lark_md_div(
    content: str,
    *,
    text_size: str = "normal",
    margin: str = "0px 0px 0px 0px",
) -> dict[str, Any]:
    return {
        "tag": "div",
        "text": {
            "tag": "lark_md",
            "content": content,
            "text_size": text_size,
            "text_align": "left",
        },
        "margin": margin,
    }


def _divider(*, margin: str = "8px 0px 8px 0px") -> dict[str, Any]:
    return {
        "tag": "hr",
        "margin": margin,
    }


def _section_title(title: str) -> dict[str, Any]:
    return _plain_text_div(
        title,
        text_size="heading",
        text_color="blue",
        margin="4px 0px 4px 0px",
    )


def _project_runtime_elements(project_context: Any) -> list[dict[str, Any]]:
    if not project_context:
        return []

    display_name = str(
        getattr(project_context, "business_project_name", "") or getattr(project_context, "running_project", "")
    ).strip()
    lines = []

    running_project = str(getattr(project_context, "running_project", "") or "").strip()
    if running_project:
        lines.append(f"- 运行的项目：`{running_project}`")
    if display_name:
        lines.append(f"- 项目名称：`{display_name}`")

    execution_path = str(getattr(project_context, "execution_path", "") or "").strip()
    project_path = str(getattr(project_context, "project_path", "") or "").strip()
    if execution_path:
        lines.append(f"- 目录：`{execution_path}`")
    elif project_path:
        lines.append(f"- 目录：`{project_path}`")
    if project_path and execution_path and project_path != execution_path:
        lines.append(f"- 主仓库：`{project_path}`")

    current_branch = str(getattr(project_context, "current_branch", "") or "").strip()
    if current_branch:
        lines.append(f"- 当前分支：`{current_branch}`")

    current_version = str(getattr(project_context, "current_version", "") or "").strip()
    if current_version:
        lines.append(f"- 当前版本：`{current_version}`")

    target_branch = str(getattr(project_context, "target_branch", "") or "").strip()
    if target_branch:
        lines.append(f"- 对齐分支：`{target_branch}`")

    target_tag = str(getattr(project_context, "target_tag", "") or "").strip()
    if target_tag:
        lines.append(f"- 对应 Tag：`{target_tag}`")

    version_field = str(getattr(project_context, "version_source_field", "") or "").strip()
    version_value = str(getattr(project_context, "version_source_value", "") or "").strip()
    if version_field and version_value:
        lines.append(f"- 版本线索：`{version_field} = {version_value}`")

    worktree_path = str(getattr(project_context, "recommended_worktree", "") or "").strip()
    if worktree_path:
        lines.append(f"- 建议 worktree：`{worktree_path}`")

    if not lines:
        return []

    return [
        _section_title("运行信息"),
        _lark_md_div("\n".join(lines)),
    ]


def _step_block(index: int, title: str, detail: str = "") -> dict[str, Any]:
    block = {
        "tag": "div",
        "text": {
            "tag": "plain_text",
            "content": f"{index}. {title}",
            "text_size": "normal",
            "text_align": "left",
            "text_color": "default",
        },
        "margin": "4px 0px 4px 0px",
    }
    detail = detail.strip()
    if detail:
        block["fields"] = [{
            "is_short": False,
            "text": {
                "tag": "lark_md",
                "content": detail,
            },
        }]
    return block


def _card_title_from_text(text: str) -> str:
    lines = [line.strip(" #\t") for line in text.splitlines() if line.strip()]
    if lines:
        first = lines[0]
        return first[:36]
    return "工作助理回复"


def _build_text_card_payload(
    text: str,
    *,
    title: str | None = None,
    variant: str = "standard",
    project_context: Any = None,
) -> tuple[str, str]:
    clean_text = (text or "").strip()
    if not clean_text:
        clean_text = "已处理。"

    paragraphs = [p.strip() for p in clean_text.split("\n\n") if p.strip()]
    summary = paragraphs[0] if paragraphs else clean_text
    details = "\n\n".join(paragraphs[1:]).strip()

    elements: list[dict[str, Any]] = []
    runtime_elements = _project_runtime_elements(project_context)
    if runtime_elements:
        elements.extend(runtime_elements)
    summary_title = "结论" if variant == "deep" else "概览"
    detail_title = "补充说明" if variant == "deep" else "说明"
    header_template = "green" if variant == "deep" else "blue"
    if elements:
        elements.append(_divider())
    elements.append(_section_title(summary_title))
    elements.append(_lark_md_div(summary))
    if details:
        elements.append(_divider())
        elements.append(_section_title(detail_title))
        elements.append(_lark_md_div(details))

    card = {
        "schema": "2.0",
        "config": {
            "wide_screen_mode": True,
        },
        "header": {
            "title": {
                "tag": "plain_text",
                "content": (title or _card_title_from_text(clean_text))[:100],
            },
            "template": header_template,
            "padding": "12px 12px 12px 12px",
        },
        "body": {
            "direction": "vertical",
            "padding": "12px 12px 12px 12px",
            "elements": elements,
        },
    }
    return _safe_json_dumps(card), clean_text


def _render_mermaid_to_png(mermaid_source: str) -> Path | None:
    mermaid_source = (mermaid_source or "").strip()
    if not mermaid_source:
        return None

    work_dir = Path(tempfile.gettempdir()) / "work-agent-os" / "mermaid"
    work_dir.mkdir(parents=True, exist_ok=True)

    digest = hashlib.sha1(mermaid_source.encode("utf-8")).hexdigest()[:16]
    source_path = work_dir / f"{digest}.mmd"
    image_path = work_dir / f"{digest}.png"
    source_path.write_text(mermaid_source, encoding="utf-8")

    if image_path.exists() and image_path.stat().st_size > 0:
        return image_path

    commands: list[list[str]] = []
    mmdc = shutil.which("mmdc")
    if mmdc:
        commands.append([mmdc, "-i", str(source_path), "-o", str(image_path), "-b", "transparent"])

    for cmd in commands:
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except Exception as e:
            logger.warning("Pipeline: mermaid render command failed to start: {}", e)
            continue

        if proc.returncode == 0 and image_path.exists() and image_path.stat().st_size > 0:
            return image_path

        stderr = (proc.stderr or proc.stdout or "").strip()
        logger.warning("Pipeline: mermaid render failed (cmd={}): {}", cmd[0], stderr[:400])

    # Network fallback via mermaid.ink.
    try:
        payload = {
            "code": mermaid_source,
            "mermaid": {"theme": "default"},
        }
        encoded = base64.urlsafe_b64encode(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).decode("ascii").rstrip("=")
        req = urlrequest.Request(
            f"https://mermaid.ink/img/{encoded}?type=png",
            headers={"User-Agent": "Mozilla/5.0"},
            method="GET",
        )
        with urlrequest.urlopen(req, timeout=30) as resp:
            png_data = resp.read()
        if png_data:
            image_path.write_bytes(png_data)
            if image_path.exists() and image_path.stat().st_size > 0:
                logger.info("Pipeline: mermaid rendered via mermaid.ink fallback")
                return image_path
    except Exception as e:
        logger.warning("Pipeline: mermaid render via mermaid.ink failed: {}", e)

    return None


def _build_structured_card_payload(
    payload: dict[str, Any],
    client: Any,
    *,
    variant: str = "standard",
    project_context: Any = None,
) -> tuple[str, str]:
    card_format = str(payload.get("format", "") or "rich").strip() or "rich"
    title = str(payload.get("title", "") or "").strip() or ("流程说明" if card_format == "flow" else "工作助理回复")
    summary = str(payload.get("summary", "") or "").strip()
    fallback_text = _reply_content_to_text(payload)
    header_template = {
        "supplement": "orange",
        "deep": "green",
        "standard": "blue",
        "flow": "blue",
    }.get(variant, "blue")

    elements: list[dict[str, Any]] = []
    runtime_elements = _project_runtime_elements(project_context)
    if runtime_elements:
        elements.extend(runtime_elements)
    if summary:
        if elements:
            elements.append(_divider())
        elements.append(_section_title("当前状态" if variant == "supplement" else "概览"))
        elements.append(_lark_md_div(summary))

    sections = payload.get("sections") or []
    if isinstance(sections, list):
        for section in sections:
            if not isinstance(section, dict):
                continue
            section_title = str(section.get("title", "") or "").strip()
            section_content = str(section.get("content", "") or "").strip()
            if not section_content:
                continue
            if elements:
                elements.append(_divider())
            if section_title:
                elements.append(_section_title(section_title))
                elements.append(_lark_md_div(section_content))
            else:
                elements.append(_lark_md_div(section_content))

    steps = payload.get("steps") or []
    if isinstance(steps, list) and steps:
        if elements:
            elements.append(_divider())
        elements.append(_section_title("步骤"))
        for idx, step in enumerate(steps, start=1):
            if isinstance(step, dict):
                step_title = str(step.get("title", "") or f"步骤{idx}").strip()
                detail = str(step.get("detail", "") or "").strip()
                elements.append(_step_block(idx, step_title, detail))
            elif step:
                elements.append(_step_block(idx, str(step)))

    table = payload.get("table")
    if isinstance(table, dict):
        normalized_table = _normalize_flow_table(table)
        if normalized_table:
            if elements:
                elements.append(_divider())
            elements.append(_section_title("检查表"))
            elements.append(normalized_table)

    mermaid = str(payload.get("mermaid", "") or "").strip()
    upload_image = getattr(client, "upload_image", None)
    if mermaid and callable(upload_image):
        image_path = _render_mermaid_to_png(mermaid)
        if image_path:
            image_key = upload_image(str(image_path))
            if isinstance(image_key, str) and image_key:
                if elements:
                    elements.append(_divider())
                elements.append(_section_title("流程图"))
                elements.append(_markdown_element(f"![流程图]({image_key})"))

    if not elements:
        elements.append(_section_title("概览"))
        elements.append(_lark_md_div(fallback_text or title))

    card = {
        "schema": "2.0",
        "config": {
            "wide_screen_mode": True,
        },
        "header": {
            "title": {
                "tag": "plain_text",
                "content": title[:100],
            },
            "template": header_template,
            "padding": "12px 12px 12px 12px",
        },
        "body": {
            "direction": "vertical",
            "padding": "12px 12px 12px 12px",
            "elements": elements,
        },
    }
    return _safe_json_dumps(card), fallback_text


def _prepare_feishu_reply(
    content: Any,
    client: Any,
    *,
    classified_type: str | None = None,
    topic: str | None = None,
    model_id: str | None = None,
    usage: dict[str, Any] | None = None,
    project_context: Any = None,
) -> tuple[str, str, str, dict[str, Any]]:
    presentation = _select_reply_presentation(
        content,
        classified_type=classified_type,
        model_id=model_id,
        usage=usage,
    )
    variant = str(presentation.get("variant") or "plain")
    if isinstance(content, dict):
        reply_format = content.get("format")
        if reply_format in {"flow", "rich"} or any(key in content for key in ("steps", "sections", "table", "mermaid")):
            body_content, db_text = _build_structured_card_payload(
                content,
                client,
                variant=variant,
                project_context=project_context,
            )
            return "interactive", body_content, db_text, presentation

        msg_type = str(content.get("msg_type") or "").strip()
        raw_content = content.get("content")

        if msg_type == "text":
            if isinstance(raw_content, dict):
                text_content = str(raw_content.get("text", "") or "")
            else:
                text_content = str(raw_content or "")
            db_text = _reply_content_to_text(content) or text_content
            return "text", text_content, db_text, presentation

        if msg_type in {"post", "interactive"}:
            if isinstance(raw_content, str):
                body_content = raw_content
            else:
                body_content = _safe_json_dumps(raw_content or {})
            return msg_type, body_content, _reply_content_to_text(content), presentation

        if msg_type == "image":
            image_key = str(content.get("image_key", "") or "").strip()
            image_path = str(content.get("image_path", "") or "").strip()
            upload_image = getattr(client, "upload_image", None)
            if not image_key and image_path and callable(upload_image):
                uploaded = upload_image(image_path)
                if isinstance(uploaded, str):
                    image_key = uploaded
            if image_key:
                return "image", _safe_json_dumps({"image_key": image_key}), _reply_content_to_text(content), presentation

    text_content = _reply_content_to_text(content)
    if variant != "plain" and _should_cardify_text_reply(text_content, classified_type):
        body_content, db_text = _build_text_card_payload(
            text_content,
            title=topic,
            variant=variant,
            project_context=project_context,
        )
        return "interactive", body_content, db_text, presentation
    return "text", text_content, text_content, presentation


def _get_default_agent_runtime() -> str:
    return normalize_agent_runtime(
        get_agent_runtime_override() or settings.default_agent_runtime or DEFAULT_AGENT_RUNTIME
    )


def _get_effective_model_for_runtime(runtime: str | None) -> str | None:
    resolved_runtime = normalize_agent_runtime(runtime or _get_default_agent_runtime())
    config = load_models_config()
    return get_model_override(resolved_runtime) or get_default_model_for_runtime(config, resolved_runtime)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def process_message(message_id: int) -> None:
    """Process a single message end-to-end.

    Messages for the same session are serialized via per-session locks to
    prevent concurrent agent resume calls on the same SDK session.
    """
    try:
        # 1. Read message
        msg = await _read_message(message_id)
        if not msg or msg["pipeline_status"] == "completed":
            return

        await _update_message(message_id, pipeline_status="classifying")

        # 2. Session routing (thread_id match or create new) — outside lock
        session_id = msg["session_id"] or await _route_session(msg)
        if session_id:
            await _update_message(message_id, session_id=session_id)
            await _attach_message_to_session(message_id, session_id)

        # 3. Acquire per-session lock, then run agent + deliver + persist
        if session_id:
            lock = await _get_session_lock(session_id)
            logger.debug("Pipeline: message {} waiting for session {} lock", message_id, session_id)
            async with lock:
                logger.debug("Pipeline: message {} acquired session {} lock", message_id, session_id)
                await _process_message_locked(message_id, msg, session_id)
        else:
            await _process_message_locked(message_id, msg, session_id)

    except Exception as e:
        logger.exception("Pipeline failed for message {}: {}", message_id, e)
        await _update_message(message_id,
                              pipeline_status="failed",
                              pipeline_error=str(e)[:500],
                              processed_at=datetime.now().isoformat())
        await _audit("pipeline_failed", "message", str(message_id),
                      {"error": str(e)[:1000]})


async def _process_message_locked(message_id: int, msg: dict,
                                   session_id: int | None) -> None:
    """Core processing under per-session lock (steps 3-11)."""
    # 3. Re-read fresh session state (may have been updated by prior message)
    session = await _read_session(session_id) if session_id else None
    analysis_dir: Path | None = None
    triage_state: dict[str, Any] | None = None
    ones_artifacts: dict[str, Any] | None = None
    if session_id and await _should_prepare_analysis_workspace(msg, session, session_id):
        analysis_dir = await _materialize_analysis_workspace(message_id, msg, session, session_id)
        refreshed = await _read_message(message_id)
        if refreshed:
            msg = refreshed
    if analysis_dir:
        triage_state = _load_triage_state(analysis_dir)
    if _ones_link_detected(msg):
        ones_artifacts = await _fetch_ones_task_artifacts(msg)
    elif analysis_dir:
        ones_artifacts = _load_ones_artifacts_from_triage_state(analysis_dir)

    agent_sid = session["agent_session_id"] if session else None
    project = session["project"] if session else ""
    agent_runtime = normalize_agent_runtime(
        (session or {}).get("agent_runtime") or _get_default_agent_runtime()
    )
    direct_project = None
    active_project = project
    project_runtime_context = None
    route_mode = "orchestrator"

    if not (agent_sid and project):
        direct_project = await _try_direct_project_route(msg, session)
        if direct_project:
            active_project = direct_project["project_name"]

    ones_check: dict[str, Any] | None = None
    is_ones_case = bool(ones_artifacts or _ones_link_detected(msg) or ((triage_state or {}).get("ones_context")))
    if is_ones_case:
        ones_check = _evaluate_ones_artifact_completeness(msg, ones_artifacts, analysis_dir)
        if analysis_dir:
            _persist_ones_context_to_triage_state(
                analysis_dir,
                ones_result=ones_artifacts,
                check=ones_check,
                project_name=active_project or project,
            )

    # 4. Audit: log before agent call
    prompt = _build_prompt(msg, session)
    if ones_artifacts:
        prompt += _build_ones_artifact_context(ones_artifacts)
    if ones_check and ones_check.get("status") == "complete":
        prompt += _build_ones_evidence_guard_context(ones_check)
    if analysis_dir:
        prompt += (
            f"\n\n[分析工作目录] {analysis_dir}"
            f"\n[附件目录] {analysis_dir / '01-intake' / 'attachments'}"
        )
    skip_agent = bool(ones_check and ones_check.get("status") != "complete")
    if skip_agent:
        route_mode = "ones_intake_gate"
        parsed = {
            "action": "replied",
            "classified_type": "work_question",
            "topic": "补充排障材料",
            "project_name": active_project or project,
            "reply_content": _build_ones_missing_info_reply(ones_check or {}, active_project or project),
            "reason": "ones_evidence_incomplete",
        }
        result = {
            "text": _safe_json_dumps(parsed),
            "session_id": agent_sid,
            "usage": {},
            "model": _get_effective_model_for_runtime(agent_runtime),
            "is_error": False,
        }
        await _audit("pipeline_ones_evidence_gate", "message", str(message_id), {
            "project_name": active_project or project,
            "missing_items": (ones_check or {}).get("missing_items", []),
            "evidence_sources": (ones_check or {}).get("evidence_sources", []),
        })
    else:
        await _audit("pipeline_agent_call", "message", str(message_id), {
            "message_id": message_id,
            "session": session,
            "prompt": prompt[:2000],
        })

        # 5. Run agent (project resume, direct ONES route, or orchestrator)
        agent_input = _build_agent_input(msg, session, agent_runtime, prompt)

        if agent_sid and project:
            route_mode = "project_resume"
            try:
                from core.projects import prepare_project_runtime_context

                project_runtime_context = prepare_project_runtime_context(project, ones_result=ones_artifacts)
            except Exception as e:
                logger.warning("Pipeline: failed to resolve runtime context for {}: {}", project, e)
                project_runtime_context = None
            result = await _run_project_agent(
                project,
                agent_input,
                agent_sid,
                runtime=agent_runtime,
                runtime_context=project_runtime_context,
            )
            # Fallback: context overflow → compact session then retry
            if (
                agent_runtime == "claude"
                and result.get("is_error")
                and result.get("text") == "Prompt is too long"
            ):
                from core.projects import get_project as _get_proj
                proj = _get_proj(project)
                cwd = str(proj.path) if proj else None
                if cwd:
                    logger.warning("Pipeline: session {} context overflow, compacting...", agent_sid)
                    compacted = await _compact_agent_session(agent_sid, cwd)
                    if compacted:
                        await _audit("context_compacted", "session", str(session_id),
                                      {"agent_session_id": agent_sid, "trigger": "overflow"})
                        result = await _run_project_agent(
                            project,
                            agent_input,
                            agent_sid,
                            runtime_context=project_runtime_context,
                        )
                    else:
                        logger.error("Pipeline: compact failed, cannot recover session {}", agent_sid)
        elif direct_project:
            active_project = direct_project["project_name"]
            route_mode = "direct_project"
            await _audit("pipeline_direct_route", "message", str(message_id), {
                "project_name": active_project,
                "confidence": direct_project["confidence"],
                "score": direct_project["score"],
                "task_ref": direct_project["task_ref"],
                "task_url": direct_project["task_url"],
                "reasons": direct_project["reasons"],
            })
            project_prompt = direct_project["prompt"]
            if ones_artifacts:
                project_prompt += _build_ones_artifact_context(ones_artifacts)
            if ones_check:
                project_prompt += _build_ones_evidence_guard_context(ones_check)
            try:
                from core.projects import prepare_project_runtime_context

                project_runtime_context = prepare_project_runtime_context(active_project, ones_result=ones_artifacts)
            except Exception as e:
                logger.warning("Pipeline: failed to resolve runtime context for {}: {}", active_project, e)
                project_runtime_context = None
            result = await _run_project_agent(
                active_project,
                project_prompt,
                None,
                runtime=agent_runtime,
                runtime_context=project_runtime_context,
            )
        else:
            result = await _run_orchestrator(agent_input, session_id=agent_sid, runtime=agent_runtime)

    result_text = result.get("text", "")
    new_agent_sid = result.get("session_id")
    if not skip_agent and active_project:
        parsed = _normalize_project_result(result_text, session=session, project=active_project)
        if _should_retry_project_response(parsed):
            logger.info("Pipeline: project reply for message {} looks like premature clarification, retrying once", message_id)
            retry_prompt = (
                "不要先向用户澄清。你必须先搜索代码、配置、日志、注释和结构化记忆，"
                "先给出你已经能确认的结论；只有在检索后仍然缺少会直接影响结论的关键上下文时，"
                "最后才允许附带一个最小追问。\n\n"
                f"用户本轮消息：{direct_project['prompt'] if direct_project else prompt}"
            )
            retry_result = await _run_project_agent(
                active_project,
                retry_prompt,
                new_agent_sid or agent_sid,
                runtime=agent_runtime,
                runtime_context=project_runtime_context,
            )
            result = retry_result
            result_text = retry_result.get("text", "")
            new_agent_sid = retry_result.get("session_id") or new_agent_sid
            parsed = _normalize_project_result(result_text, session=session, project=active_project)
    else:
        parsed = _parse_result(result_text)

    # 6. Audit: log agent result
    # Read effective agent_session_id from DB (dispatch_to_project may have
    # written the project agent's session_id directly, which differs from
    # the orchestrator's own session_id returned in new_agent_sid).
    effective_agent_sid = new_agent_sid
    if session_id:
        fresh = await _read_session(session_id)
        if fresh and fresh.get("agent_session_id"):
            effective_agent_sid = fresh["agent_session_id"]

    if not skip_agent:
        await _audit("pipeline_agent_result", "message", str(message_id), {
            "agent_session_id": effective_agent_sid,
            "action": parsed.get("action"),
            "classified_type": parsed.get("classified_type"),
            "result_text": result_text[:1500],
        })

    # 7. Deliver reply via feishu (pipeline handles this, not agent)
    reply_content = parsed.get("reply_content", "")
    action = parsed.get("action", "replied")
    thread_id = None
    delivered = True

    if action in ("replied", "drafted"):
        if not reply_content:
            # Agent said replied but gave no content — treat as failure
            await _update_message(message_id,
                                  pipeline_status="failed",
                                  pipeline_error="action=replied but reply_content is empty",
                                  processed_at=datetime.now().isoformat())
            await _audit("pipeline_feishu_reply", "message", str(message_id), {
                "delivered": False,
                "action": action,
                "error": "empty reply_content",
            })
            logger.warning("Pipeline: message {} marked failed — empty reply_content", message_id)
            return
        if not msg["platform_message_id"]:
            await _update_message(message_id,
                                  pipeline_status="failed",
                                  pipeline_error="missing platform_message_id, cannot deliver",
                                  processed_at=datetime.now().isoformat())
            logger.warning("Pipeline: message {} marked failed — no platform_message_id", message_id)
            return
        project_name_for_reply = str(parsed.get("project_name") or active_project or project or "").strip()
        if _contains_internal_tool_error(reply_content):
            original_reply = _reply_content_to_text(reply_content)
            reply_content = _build_internal_tool_error_reply(
                classified_type=parsed.get("classified_type"),
                project_name=project_name_for_reply,
            )
            parsed["reply_content"] = reply_content
            await _audit("pipeline_internal_tool_error_sanitized", "message", str(message_id), {
                "project_name": project_name_for_reply,
                "original_reply": original_reply[:500],
                "replacement_reply": reply_content,
                "route_mode": route_mode,
            })

        project_context = None
        if project_name_for_reply:
            try:
                from core.projects import prepare_project_runtime_context

                project_context = prepare_project_runtime_context(
                    project_name_for_reply,
                    ones_result=ones_artifacts,
                )
            except Exception as e:
                logger.warning("Pipeline: failed to build project runtime context for {}: {}", project_name_for_reply, e)

        thread_id, delivered = await _deliver_reply(
            msg,
            reply_content,
            session_id,
            classified_type=parsed.get("classified_type"),
            topic=parsed.get("topic"),
            reply_model=result.get("model"),
            reply_usage=result.get("usage"),
            project_context=project_context,
        )
        await _audit("pipeline_feishu_reply", "message", str(message_id), {
            "delivered": delivered,
            "action": action,
            "thread_id": thread_id,
            "content_length": len(_reply_content_to_text(reply_content)),
        })
        if not delivered:
            await _update_message(message_id,
                                  pipeline_status="failed",
                                  pipeline_error="feishu reply delivery failed",
                                  processed_at=datetime.now().isoformat())
            logger.warning("Pipeline: message {} marked failed — feishu delivery failed", message_id)
            return

    # 8. Persist session state (agent_session_id, project, thread_id)
    if session_id:
        await _update_session_state(
            session_id,
            agent_session_id=new_agent_sid,
            agent_runtime=agent_runtime,
            project=parsed.get("project_name") or active_project or "",
            thread_id=thread_id,
        )

    trace_requested = analysis_dir is not None or _should_capture_process_trace(msg, session, parsed)
    if session_id and trace_requested:
        try:
            trace_session = await _read_session(session_id)
            if trace_session:
                analysis_dir = await _materialize_analysis_workspace(message_id, msg, trace_session, session_id)
                effective_agent_sid = (
                    str((trace_session.get("agent_session_id") or effective_agent_sid or new_agent_sid or "")).strip()
                )
                analysis_trace = _extract_rollout_trace(agent_runtime, effective_agent_sid or new_agent_sid)
                _write_process_trace_artifacts(
                    analysis_dir,
                    message_id=message_id,
                    session_id=session_id,
                    msg=msg,
                    session=trace_session,
                    prompt=prompt,
                    agent_runtime=agent_runtime,
                    pre_project=project,
                    route_mode=route_mode,
                    direct_project=direct_project,
                    ones_artifacts=ones_artifacts,
                    parsed=parsed,
                    result_text=result_text,
                    new_agent_sid=new_agent_sid,
                    effective_agent_sid=effective_agent_sid,
                    analysis_trace=analysis_trace,
                )
        except Exception as e:
            logger.warning("Pipeline: failed to persist analysis trace for message {}: {}", message_id, e)

    # 9. Mark completed
    await _update_message(message_id,
                          pipeline_status="completed",
                          classified_type=parsed.get("classified_type", "chat"),
                          processed_at=datetime.now().isoformat(),
                          pipeline_error="")

    # 10. Record agent run
    await _record_agent_run(message_id, session_id, new_agent_sid, agent_runtime, result)

    await _audit("pipeline_completed", "message", str(message_id), {
        "action": action,
        "classified_type": parsed.get("classified_type"),
        "session_id": session_id,
    })
    logger.info("Pipeline: message {} → {} ({})",
                 message_id, action, parsed.get("classified_type"))

    # 11. Proactive compaction — also under session lock (runs in background
    #     but acquires the same lock so it won't overlap with the next message)
    effective_sid = effective_agent_sid or new_agent_sid
    if effective_sid and active_project and agent_runtime == "claude":
        input_tokens = _get_input_tokens(result)
        ctx_window = _get_context_window(result.get("model"))
        if input_tokens > ctx_window * _COMPACT_THRESHOLD:
            from core.projects import get_project as _get_proj
            proj = _get_proj(active_project)
            if proj:
                logger.info("Pipeline: proactive compact for session {} "
                            "(tokens={}, threshold={})",
                            effective_sid, input_tokens, int(ctx_window * _COMPACT_THRESHOLD))
                asyncio.create_task(
                    _proactive_compact_locked(session_id, effective_sid, str(proj.path))
                )


async def reprocess_message(message_id: int) -> None:
    await _update_message(message_id,
                          pipeline_status="pending",
                          pipeline_error="",
                          classified_type=None,
                          session_id=None,
                          processed_at=None)
    await process_message(message_id)


# ---------------------------------------------------------------------------
# Context compaction
# ---------------------------------------------------------------------------

# Model context window sizes (tokens)
_CONTEXT_WINDOWS = {
    "haiku": 200_000,
    "sonnet": 200_000,
    "opus": 1_000_000,
}
_COMPACT_THRESHOLD = 0.6  # Compact when input exceeds 60% of context window
_COMPACT_MODEL = "claude-opus-4-6"  # Large model used for compaction


def _get_context_window(model_id: str | None) -> int:
    """Estimate context window from model id string."""
    if not model_id:
        return 200_000
    model_lower = model_id.lower()
    for key, size in _CONTEXT_WINDOWS.items():
        if key in model_lower:
            return size
    return 200_000  # Conservative default


async def _compact_agent_session(session_id: str, cwd: str,
                                  model: str | None = None) -> bool:
    """Send /compact to an agent session via SDK. Returns True on success."""
    from claude_agent_sdk import query as sdk_query, ClaudeAgentOptions, ResultMessage

    opts = ClaudeAgentOptions(
        permission_mode="bypassPermissions",
        max_turns=10,
        cwd=cwd,
        model=model or _COMPACT_MODEL,
    )
    opts.resume = session_id

    try:
        async for msg in sdk_query(prompt="/compact", options=opts):
            if isinstance(msg, ResultMessage):
                if msg.is_error:
                    logger.error("Compact failed for session {}: is_error=True", session_id)
                    return False
        logger.info("Compact completed for session {}", session_id)
        return True
    except Exception as e:
        logger.error("Compact exception for session {}: {}", session_id, e)
        return False


async def _proactive_compact_locked(db_session_id: int,
                                     agent_session_id: str, cwd: str) -> None:
    """Background task: acquire session lock, then compact."""
    lock = await _get_session_lock(db_session_id)
    async with lock:
        try:
            ok = await _compact_agent_session(agent_session_id, cwd)
            if ok:
                await _audit("context_compacted", "session", str(db_session_id),
                              {"agent_session_id": agent_session_id, "trigger": "proactive"})
        except Exception as e:
            logger.error("Proactive compact failed for {}: {}", agent_session_id, e)


def _get_input_tokens(result: dict) -> int:
    """Extract total input tokens from agent result usage."""
    usage = result.get("usage", {})
    return (usage.get("input_tokens", 0)
            + usage.get("cache_read_input_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0))


# ---------------------------------------------------------------------------
# Agent execution
# ---------------------------------------------------------------------------

async def _run_orchestrator(
    prompt: str | AsyncIterable[dict[str, Any]],
    session_id: str | None = None,
    runtime: str | None = None,
) -> dict:
    from core.orchestrator.agent_client import agent_client
    return await agent_client.run(
        prompt=prompt,
        system_prompt=_build_system_prompt(),
        max_turns=30,
        session_id=session_id,
        runtime=runtime,
    )


async def _run_project_agent(
    project_name: str,
    prompt: str | AsyncIterable[dict[str, Any]],
    session_id: str | None,
    runtime: str | None = None,
    runtime_context: Any = None,
) -> dict:
    from core.orchestrator.agent_client import agent_client
    from core.orchestrator.agent_client import PROJECT_AGENT_RESPONSE_RULES
    from core.projects import (
        build_project_runtime_prompt_block,
        get_project,
        merge_skills,
        prepare_project_runtime_context,
    )
    from skills import SKILL_REGISTRY

    project = get_project(project_name)
    if not project:
        logger.warning("Project {} not found, falling back to orchestrator", project_name)
        return await _run_orchestrator(prompt, runtime=runtime)

    runtime_context = runtime_context or prepare_project_runtime_context(project_name)
    runtime_block = build_project_runtime_prompt_block(runtime_context)
    merged = merge_skills(SKILL_REGISTRY, project.path, include_global=True)
    system = (
        f"你运行在项目 {project.name} 的工作目录中（{project.path}）。处理用户的请求。"
        f"{runtime_block}"
        f"\n\n{PROJECT_AGENT_RESPONSE_RULES}"
    )

    prompt_preview = prompt[:100] if isinstance(prompt, str) else "[multimodal prompt]"
    logger.info("_run_project_agent: project={}, session_id={}, cwd={}, prompt={}",
                project_name, session_id, str(project.path), prompt_preview)

    return await agent_client.run_for_project(
        prompt=prompt,
        system_prompt=system,
        project_cwd=str(getattr(runtime_context, "execution_path", project.path)),
        project_agents=merged,
        max_turns=20,
        session_id=session_id,
        runtime=runtime,
    )


async def _try_direct_project_route(msg: dict, session: dict | None) -> dict[str, Any] | None:
    if session and session.get("project"):
        return None
    if session and int(session.get("message_count") or 0) > 1:
        return None

    content = str(msg.get("content") or "").strip()
    if not content or "ones." not in content.lower():
        return None

    from core.ones_routing import extract_ones_task_link, try_direct_project_route

    if not extract_ones_task_link(content):
        return None

    decision = await try_direct_project_route(content)
    if not decision:
        return None

    return {
        "project_name": decision.project_name,
        "prompt": decision.build_project_prompt(content),
        "confidence": decision.confidence,
        "score": decision.score,
        "reasons": decision.reasons,
        "task_ref": decision.task_ref,
        "task_url": decision.task_url,
        "task_summary": decision.task_summary,
    }


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------

def _build_prompt(msg: dict, session: dict | None) -> str:
    """Build prompt from message and session context."""
    content = msg["content"] or ""
    media = _extract_media(msg)

    # Project resume: just the user message
    if session and session.get("agent_session_id") and session.get("project"):
        prompt = content
        if media:
            prompt += f"\n\n[多模态内容] {media}"
        return prompt

    # New or non-project: include IDs for orchestrator
    parts = [f"飞书消息ID: {msg['platform_message_id']} | db_session_id: {session['id'] if session else ''}"]
    if media:
        parts.append(f"[多模态内容] {media}")
    parts.append("")
    parts.append(content)
    return "\n".join(parts)


def _parse_media_info(msg: dict) -> dict[str, Any]:
    raw = msg.get("media_info_json") or ""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _build_agent_input(
    msg: dict,
    session: dict | None,
    runtime: str,
    prompt_text: str,
) -> str | AsyncIterable[dict[str, Any]]:
    if runtime != "claude":
        return prompt_text

    media_info = _parse_media_info(msg)
    media_type = str(media_info.get("type") or "")
    if media_type == "image":
        local_path = str(media_info.get("local_path") or msg.get("attachment_path") or "").strip()
    elif media_type == "post" and media_info.get("has_image"):
        local_paths = media_info.get("local_paths") or []
        local_path = str(local_paths[0] if local_paths else media_info.get("local_path") or "").strip()
    else:
        return prompt_text

    if not local_path:
        return prompt_text

    image_path = Path(local_path)
    if not image_path.exists():
        return prompt_text

    mime_type = str(media_info.get("mime_type") or "").strip() or (
        mimetypes.guess_type(image_path.name)[0] or "image/png"
    )

    try:
        image_b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
    except OSError as e:
        logger.warning("Pipeline: failed to read image attachment {}: {}", image_path, e)
        return prompt_text

    payload = {
        "type": "user",
        "session_id": "",
        "message": {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt_text},
                {"type": "image", "data": image_b64, "mimeType": mime_type},
            ],
        },
        "parent_tool_use_id": None,
    }

    class _SinglePromptStream:
        def __aiter__(self):
            async def _gen():
                yield payload

            return _gen()

    return _SinglePromptStream()


_ANALYSIS_KEYWORDS = (
    "分析",
    "排查",
    "定位",
    "根因",
    "原因",
    "结论",
    "帮我看",
    "帮忙看",
    "看下问题",
    "看下日志",
    "日志分析",
    "异常堆栈",
    "5why",
    "5 why",
)

_REVIEW_KEYWORDS = (
    "review",
    "code review",
    "merge request",
    "mr 评论",
    "mr评论",
    "行评论",
    "可合并",
    "能合并",
    "cherry-pick",
    "cherrypick",
    "审查",
)

_GITLAB_ISSUE_URL_RE = re.compile(r"https?://[^\s]+/-/issues/\d+", flags=re.IGNORECASE)
_GITLAB_MR_URL_RE = re.compile(r"https?://[^\s]+/-/merge_requests/\d+", flags=re.IGNORECASE)


def _analysis_requested(msg: dict) -> bool:
    content = str(msg.get("content") or "").strip().lower()
    media_info = _parse_media_info(msg)
    if any(keyword in content for keyword in _ANALYSIS_KEYWORDS):
        return True
    if media_info.get("type") in {"image", "post"} and (
        ("图片" in content and "什么" in content)
        or ("图" in content and "什么" in content)
        or ("识别" in content)
        or ("看图" in content)
    ):
        return True
    return False


def _review_requested(msg: dict) -> bool:
    content = str(msg.get("content") or "").strip()
    lowered = content.lower()
    if _GITLAB_ISSUE_URL_RE.search(content) or _GITLAB_MR_URL_RE.search(content):
        return True
    return any(keyword in lowered for keyword in _REVIEW_KEYWORDS)


def _slugify_text(value: str, max_length: int = 48) -> str:
    normalized = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in (value or "").strip())
    normalized = "-".join(part for part in normalized.split("-") if part)
    return (normalized or "analysis")[:max_length].rstrip("-_")


def _triage_base_dir() -> Path:
    return settings.project_root / ".triage"


def _review_base_dir() -> Path:
    return settings.project_root / ".review"


def _analysis_dir_for_session(session_id: int, session: dict | None, msg: dict) -> Path:
    existing = str((session or {}).get("analysis_workspace") or "").strip()
    if existing:
        return Path(existing)
    seed = (session or {}).get("title") or (session or {}).get("topic") or str(msg.get("content") or "")
    slug = _slugify_text(seed)
    base_dir = _review_base_dir() if _review_requested(msg) else _triage_base_dir()
    return base_dir / f"session-{session_id}-{slug}"


def _has_media(msg: dict) -> bool:
    media_info = _parse_media_info(msg)
    return bool(media_info.get("type"))


async def _session_has_analysis_workspace(session_id: int) -> bool:
    prefix = f"session-{session_id}-"
    for base in (_triage_base_dir(), _review_base_dir()):
        if not base.exists():
            continue
        if any(child.is_dir() and child.name.startswith(prefix) for child in base.iterdir()):
            return True
    return False


async def _should_prepare_analysis_workspace(msg: dict, session: dict | None, session_id: int | None) -> bool:
    if not session_id:
        return False
    if session and bool(session.get("analysis_mode")):
        return True
    if await _session_has_analysis_workspace(session_id):
        return True
    if _review_requested(msg):
        return True
    return _analysis_requested(msg)


async def _bind_session_analysis_workspace(session_id: int, triage_dir: Path) -> None:
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE sessions SET analysis_mode = 1, analysis_workspace = ?, updated_at = ? WHERE id = ?",
                (str(triage_dir.resolve()), datetime.now().isoformat(), session_id),
            )
            await db.commit()
    except Exception as e:
        logger.warning("Pipeline: failed to bind analysis workspace for session {}: {}", session_id, e)


def _triage_state_payload(session_id: int, session: dict | None, msg: dict, triage_dir: Path) -> dict[str, Any]:
    now = datetime.now().isoformat()
    project_runtime: dict[str, Any] = {}
    project_name = str((session or {}).get("project") or "").strip()
    if project_name:
        try:
            from core.projects import resolve_project_runtime_context

            runtime_context = resolve_project_runtime_context(project_name)
            project_runtime = runtime_context.to_payload() if runtime_context else {}
        except Exception:
            project_runtime = {}
    return {
        "project": (session or {}).get("project", ""),
        "project_runtime": project_runtime,
        "mode": "structured",
        "phase": "initialized",
        "problem_summary": ((session or {}).get("title") or msg.get("content") or "")[:200],
        "version_info": {"value": "", "status": "missing"},
        "artifact_completeness": {"status": "unknown", "notes": []},
        "time_alignment": {"problem_time": "", "log_time_format": "", "timezone": "", "status": "unknown"},
        "module_hypothesis": [],
        "target_log_files": [],
        "call_chain_status": "pending",
        "keyword_package_status": "pending",
        "search_status": "pending",
        "evidence_chain_status": "weak",
        "confidence": "low",
        "missing_items": [],
        "user_confirmation": {"formal_report": False},
        "search_artifacts": {"last_result_json": "", "last_result_md": ""},
        "session_id": session_id,
        "work_dir": str(triage_dir.resolve()),
        "created_at": now,
        "updated_at": now,
    }


def _triage_state_path(triage_dir: Path) -> Path:
    return triage_dir / "00-state.json"


def _load_triage_state(triage_dir: Path | None) -> dict[str, Any] | None:
    if not triage_dir:
        return None
    state_path = _triage_state_path(triage_dir)
    if not state_path.exists():
        return None
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _save_triage_state(triage_dir: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = datetime.now().isoformat()
    _triage_state_path(triage_dir).write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_json_artifact(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_text_artifact(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.rstrip() + "\n", encoding="utf-8")


def _truncate_text(value: Any, limit: int = 800) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _ones_link_detected(msg: dict) -> bool:
    content = str(msg.get("content") or "").strip()
    if not content or "ones." not in content.lower():
        return False
    try:
        from core.ones_routing import extract_ones_task_link
    except Exception:
        return False
    return bool(extract_ones_task_link(content))


def _extract_ones_link(msg: dict) -> str:
    content = str(msg.get("content") or "").strip()
    if not content:
        return ""
    try:
        from core.ones_routing import extract_ones_task_link
    except Exception:
        return ""
    extracted = extract_ones_task_link(content)
    return extracted[2] if extracted else ""


def _find_existing_ones_task_artifacts(task_ref: str) -> dict[str, Any] | None:
    ones_root = settings.project_root / ".ones"
    if not task_ref or not ones_root.exists():
        return None
    for task_dir in sorted(ones_root.glob(f"*_{task_ref}")):
        task_json_path = task_dir / "task.json"
        if not task_json_path.exists():
            continue
        try:
            payload = json.loads(task_json_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, ValueError):
            continue
        return payload if isinstance(payload, dict) else None
    return None


def _collect_ones_downloaded_files(ones_result: dict[str, Any]) -> list[dict[str, str]]:
    paths = (ones_result.get("paths") or {}) if isinstance(ones_result, dict) else {}
    messages_json_path = str(paths.get("messages_json") or "").strip()
    if not messages_json_path:
        return []
    try:
        payload = json.loads(Path(messages_json_path).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, ValueError):
        return []

    files: list[dict[str, str]] = []
    for item in payload.get("description_images", []) or []:
        path = str(item.get("path") or "").strip()
        if path:
            files.append({
                "label": str(item.get("label") or "").strip() or Path(path).name,
                "path": path,
                "uuid": str(item.get("uuid") or "").strip(),
            })
    for item in payload.get("attachment_downloads", []) or []:
        path = str(item.get("path") or "").strip()
        if path:
            files.append({
                "label": str(item.get("label") or "").strip() or Path(path).name,
                "path": path,
                "uuid": str(item.get("uuid") or "").strip(),
            })
    return files


def _ones_cli_script_path() -> Path:
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    return codex_home / "skills" / "ones" / "scripts" / "ones_cli.py"


async def _fetch_ones_task_artifacts(msg: dict) -> dict[str, Any] | None:
    task_url = _extract_ones_link(msg)
    if not task_url:
        return None

    try:
        from core.ones_routing import extract_ones_task_link
    except Exception:
        return None

    extracted = extract_ones_task_link(task_url)
    if not extracted:
        return None
    _, task_ref, _ = extracted

    existing = _find_existing_ones_task_artifacts(task_ref)
    if existing:
        return existing

    script_path = _ones_cli_script_path()
    if not script_path.exists():
        logger.warning("Pipeline: ONES CLI not found at {}", script_path)
        return None

    def _run_fetch() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(script_path), "fetch-task", task_url],
            cwd=str(settings.project_root),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

    try:
        completed = await asyncio.to_thread(_run_fetch)
    except Exception as e:
        logger.warning("Pipeline: ONES fetch-task failed for {}: {}", task_ref, e)
        return None

    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        stdout = (completed.stdout or "").strip()
        logger.warning(
            "Pipeline: ONES fetch-task non-zero for {}: rc={} stdout={} stderr={}",
            task_ref,
            completed.returncode,
            _truncate_text(stdout, limit=400),
            _truncate_text(stderr, limit=400),
        )
        return None

    try:
        payload = json.loads(completed.stdout or "{}")
    except (json.JSONDecodeError, TypeError, ValueError):
        logger.warning("Pipeline: ONES fetch-task output not valid JSON for {}", task_ref)
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("success") is False:
        logger.warning("Pipeline: ONES fetch-task reported failure for {}: {}", task_ref, payload.get("error"))
        return None
    data = payload.get("data")
    return data if isinstance(data, dict) else payload


def _build_ones_artifact_context(ones_result: dict[str, Any] | None) -> str:
    if not ones_result:
        return ""

    task = ones_result.get("task") or {}
    project = ones_result.get("project") or {}
    counts = ones_result.get("counts") or {}
    paths = ones_result.get("paths") or {}
    downloaded = _collect_ones_downloaded_files(ones_result)
    payload = {
        "task": {
            "number": task.get("number"),
            "uuid": task.get("uuid"),
            "summary": task.get("summary"),
            "status_name": task.get("status_name"),
            "issue_type_name": task.get("issue_type_name"),
            "url": task.get("url"),
        },
        "project": {
            "display_name": project.get("display_name"),
            "business_project_name": project.get("business_project_name"),
            "confidence": project.get("confidence"),
        },
        "counts": counts,
        "paths": {
            "task_dir": paths.get("task_dir", ""),
            "task_json": paths.get("task_json", ""),
            "messages_json": paths.get("messages_json", ""),
            "report_md": paths.get("report_md", ""),
        },
        "downloaded_files": downloaded[:12],
    }
    return "\n\n[ONES 下载产物]\n" + json.dumps(payload, ensure_ascii=False, indent=2)


_LOG_ARTIFACT_SUFFIXES = (
    ".log",
    ".txt",
    ".zip",
    ".gz",
    ".tar",
    ".tgz",
    ".bz2",
    ".xz",
    ".7z",
)
_CONFIG_EVIDENCE_KEYWORDS = (
    "配置",
    "config",
    "server.ssl",
    "jetty",
    "nginx",
    "端口",
    "证书",
    "ssl",
    "开关",
    "参数",
    "env",
    "yaml",
    "yml",
    "properties",
)
_INCIDENT_TIME_KEYWORDS = (
    "发生问题时间",
    "问题时间",
    "故障时间",
    "发生时间",
    "时间点",
)
_IDENTIFIER_KEYWORDS = (
    "订单",
    "任务号",
    "任务id",
    "车辆",
    "车号",
    "车体",
    "设备",
    "机器人",
    "request id",
    "request_id",
    "trace id",
    "trace_id",
    "号车",
    "序列号",
    "sn",
)


def _stringify_field_value(value: Any) -> str:
    if isinstance(value, list):
        return " ".join(str(item) for item in value if item is not None)
    if value is None:
        return ""
    return str(value)


def _collect_ones_text_fragments(ones_result: dict[str, Any] | None, msg: dict | None = None) -> list[str]:
    fragments: list[str] = []
    if msg:
        content = str(msg.get("content") or "").strip()
        if content:
            fragments.append(content)

    if not isinstance(ones_result, dict):
        return fragments

    task = ones_result.get("task") or {}
    for key in ("summary", "description", "desc"):
        value = str(task.get(key) or "").strip()
        if value:
            fragments.append(value)

    named_fields = ones_result.get("named_fields") or {}
    if isinstance(named_fields, dict):
        for key, value in named_fields.items():
            rendered = _stringify_field_value(value).strip()
            if rendered:
                fragments.append(f"{key}: {rendered}")

    project = ones_result.get("project") or {}
    for key in ("display_name", "business_project_name", "ones_project_name"):
        value = str(project.get(key) or "").strip()
        if value:
            fragments.append(value)

    return fragments


def _iter_analysis_workspace_files(analysis_dir: Path | None) -> list[Path]:
    if not analysis_dir:
        return []
    attachments_root = analysis_dir / "01-intake" / "attachments"
    if not attachments_root.exists():
        return []
    return [path for path in attachments_root.rglob("*") if path.is_file()]


def _looks_like_log_artifact(label: str, path: str) -> bool:
    merged = " ".join(part for part in (label, Path(path).name) if part).lower()
    if any(merged.endswith(suffix) for suffix in _LOG_ARTIFACT_SUFFIXES):
        return True
    return any(token in merged for token in ("log", "trace", "stack", "dump", "stderr", "stdout"))


def _looks_like_config_artifact(label: str, path: str) -> bool:
    merged = " ".join(part for part in (label, Path(path).name) if part).lower()
    return any(token in merged for token in ("config", "配置", "yaml", "yml", "properties", "toml", "ini", "env"))


def _contains_problem_time(text: str) -> bool:
    lowered = text.lower()
    if any(keyword in text for keyword in _INCIDENT_TIME_KEYWORDS):
        return True
    patterns = (
        r"20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}(?:[日号\sT]+)?\d{1,2}:\d{2}(?::\d{2})?",
        r"\d{1,2}[-/.月]\d{1,2}(?:[日号\sT]+)?\d{1,2}:\d{2}(?::\d{2})?",
    )
    return any(re.search(pattern, text) for pattern in patterns) or "timezone" in lowered or "时区" in text


def _contains_business_identifier(text: str) -> bool:
    lowered = text.lower()
    if any(keyword in lowered for keyword in _IDENTIFIER_KEYWORDS):
        return True
    patterns = (
        r"\b(order|task|vehicle|device|request|trace)[-_ ]?[a-z0-9]{2,}\b",
        r"\d+号车",
        r"[A-Z]{2,}-\d{2,}",
    )
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


def _needs_config_evidence(text: str) -> bool:
    lowered = text.lower()
    return any(keyword in text or keyword in lowered for keyword in _CONFIG_EVIDENCE_KEYWORDS)


def _ones_requirement_row(item: str) -> tuple[str, str]:
    mapping = {
        "问题描述": ("避免只靠标题或猜测分析", "ONES 现象描述、复现步骤、现场表现"),
        "问题发生时间": ("需要校准日志时间窗和时区", "精确到分钟的故障时间，最好带时区"),
        "相关日志/异常堆栈": ("证据链不能只靠本地历史日志", "原始日志包、故障时间窗日志、异常堆栈"),
        "订单/车辆/设备等关键信息": ("需要把现象和业务对象绑定", "订单号、任务号、车辆号、设备号、序列号"),
        "相关配置截图/配置片段": ("配置类问题必须有配置证据", "配置截图、配置文件片段、环境参数"),
    }
    return mapping.get(item, ("需要补齐关键证据", "请提供能直接支撑结论的原始材料"))


def _evaluate_ones_artifact_completeness(
    msg: dict,
    ones_result: dict[str, Any] | None,
    analysis_dir: Path | None = None,
) -> dict[str, Any]:
    fragments = _collect_ones_text_fragments(ones_result, msg=msg)
    issue_fragments = _collect_ones_text_fragments(ones_result, msg=None)
    issue_text = "\n".join(fragment for fragment in (issue_fragments or fragments) if fragment).strip()
    merged_text = "\n".join(fragment for fragment in fragments if fragment).strip()

    downloaded_files = _collect_ones_downloaded_files(ones_result or {})
    workspace_files = _iter_analysis_workspace_files(analysis_dir)
    file_records = downloaded_files + [
        {
            "label": path.name,
            "path": str(path),
            "uuid": "",
        }
        for path in workspace_files
    ]

    has_description = len(issue_text) >= 40
    has_problem_time = _contains_problem_time(issue_text)
    has_log_artifact = any(
        _looks_like_log_artifact(str(item.get("label") or ""), str(item.get("path") or ""))
        for item in file_records
    ) or any(token in issue_text.lower() for token in ("exception", "stack trace", "stacktrace", "报错", "错误码"))
    has_identifier = _contains_business_identifier(issue_text)
    needs_config = _needs_config_evidence(issue_text)
    has_config_evidence = any(
        _looks_like_config_artifact(str(item.get("label") or ""), str(item.get("path") or ""))
        for item in file_records
    ) or any(token in issue_text.lower() for token in ("server.ssl", "jetty", "nginx", "配置", "参数"))

    requirements = [
        {"key": "description", "label": "问题描述", "present": has_description},
        {"key": "problem_time", "label": "问题发生时间", "present": has_problem_time},
        {"key": "related_logs", "label": "相关日志/异常堆栈", "present": has_log_artifact},
        {"key": "business_identifier", "label": "订单/车辆/设备等关键信息", "present": has_identifier},
    ]
    if needs_config:
        requirements.append({"key": "config_evidence", "label": "相关配置截图/配置片段", "present": has_config_evidence})

    missing_items = [item["label"] for item in requirements if not item["present"]]
    status = "complete" if not missing_items else "partial"

    evidence_sources: list[str] = []
    if ones_result:
        evidence_sources.append("ONES 工单描述")
    if downloaded_files:
        evidence_sources.append(f"ONES 附件 {len(downloaded_files)} 个")
    if workspace_files:
        evidence_sources.append(f"会话附件 {len(workspace_files)} 个")

    notes = []
    if missing_items:
        notes.append("当前证据还不能稳定闭合 ONES 问题的证据链。")
    notes.append("现场证据只认当前 ONES 工单和当前会话内提供的材料，本地历史日志只能作为实现参考。")

    return {
        "status": status,
        "requirements": requirements,
        "missing_items": missing_items,
        "needs_config_evidence": needs_config,
        "problem_time_detected": has_problem_time,
        "has_related_logs": has_log_artifact,
        "has_business_identifier": has_identifier,
        "evidence_sources": evidence_sources,
        "file_records": file_records[:20],
        "notes": notes,
    }


def _build_ones_missing_info_reply(check: dict[str, Any], project_name: str = "") -> dict[str, Any]:
    received_lines = check.get("evidence_sources") or ["仅收到 ONES 链接或简要描述"]
    missing_items = [str(item).strip() for item in (check.get("missing_items") or []) if str(item).strip()]

    rows = []
    request_lines: list[str] = []
    for index, item in enumerate(missing_items, start=1):
        why, accepted = _ones_requirement_row(item)
        rows.append({"item": item, "why": why, "accepted": accepted})
        request_lines.append(f"{index}. {item}：{accepted}")

    summary = "这类 ONES 问题要先补齐最小证据链，再开始分析，避免把本地历史日志误当成现场证据。"
    if project_name:
        summary = f"已识别项目为 {project_name}。{summary}"

    fallback_lines = [
        "当前信息还不完整，先补齐证据再分析。",
        "已收到：" + "；".join(received_lines),
    ]
    if request_lines:
        fallback_lines.append("请优先补充：")
        fallback_lines.extend(request_lines)

    return {
        "format": "rich",
        "reply_style": "supplement",
        "title": "证据暂不完整，先补齐材料",
        "summary": summary,
        "sections": [
            {"title": "当前已收到", "content": "\n".join(f"- {line}" for line in received_lines)},
            {"title": "当前缺口", "content": "\n".join(f"- {item}" for item in missing_items) or "- 暂无"},
            {"title": "请优先补充", "content": "\n".join(request_lines) or "补充能直接支撑结论的原始材料。"},
        ],
        "table": {
            "columns": [
                {"key": "item", "label": "补充项", "type": "text"},
                {"key": "why", "label": "为什么需要", "type": "markdown"},
                {"key": "accepted", "label": "可接受证据", "type": "markdown"},
            ],
            "rows": rows,
        },
        "fallback_text": "\n".join(fallback_lines),
    }


def _build_ones_evidence_guard_context(check: dict[str, Any] | None) -> str:
    if not check:
        return ""
    payload = {
        "gate_status": "ready" if check.get("status") == "complete" else "awaiting_input",
        "missing_items": check.get("missing_items") or [],
        "evidence_sources": check.get("evidence_sources") or [],
        "rules": [
            "先检查证据完整性，再开始分析。",
            "现场证据只能来自当前 ONES 工单、当前会话补料和仓库内能确认的代码/配置事实。",
            "不要把本机或仓库里其他无关历史日志当成现场证据；若引用这类日志，只能标注为实现参考。",
        ],
    }
    return "\n\n[ONES 证据约束]\n" + json.dumps(payload, ensure_ascii=False, indent=2)


def _persist_ones_context_to_triage_state(
    triage_dir: Path,
    *,
    ones_result: dict[str, Any] | None,
    check: dict[str, Any],
    project_name: str = "",
) -> None:
    state = _load_triage_state(triage_dir)
    if not state:
        return

    paths = (ones_result or {}).get("paths") or {}
    task = (ones_result or {}).get("task") or {}
    state["project"] = project_name or state.get("project", "")
    state["phase"] = "completeness_checked" if check.get("status") == "complete" else "awaiting_input"
    state["artifact_completeness"] = {
        "status": check.get("status") or "unknown",
        "notes": check.get("notes") or [],
    }
    state["evidence_chain_status"] = "partial" if check.get("status") == "complete" else "weak"
    state["confidence"] = "medium" if check.get("status") == "complete" else "low"
    state["missing_items"] = check.get("missing_items") or []
    state["target_log_files"] = [
        str(Path(item.get("path") or "").name)
        for item in (check.get("file_records") or [])
        if _looks_like_log_artifact(str(item.get("label") or ""), str(item.get("path") or ""))
    ][:8]
    state["ones_context"] = {
        "task_ref": str(task.get("uuid") or ""),
        "task_url": str(task.get("url") or ""),
        "task_json": str(paths.get("task_json") or ""),
        "report_md": str(paths.get("report_md") or ""),
        "messages_json": str(paths.get("messages_json") or ""),
        "evidence_sources": check.get("evidence_sources") or [],
    }
    if project_name:
        try:
            from core.projects import resolve_project_runtime_context

            runtime_context = resolve_project_runtime_context(project_name, ones_result=ones_result)
            if runtime_context:
                state["project_runtime"] = runtime_context.to_payload()
                if runtime_context.version_source_value or runtime_context.current_version:
                    state["version_info"] = {
                        "value": runtime_context.version_source_value or runtime_context.current_version,
                        "normalized": runtime_context.normalized_version,
                        "branch": runtime_context.target_branch or runtime_context.current_branch,
                        "tag": runtime_context.target_tag,
                        "status": "resolved" if runtime_context.version_source_value else "inferred",
                    }
            else:
                state["project_runtime"] = {}
        except Exception as e:
            logger.warning("Pipeline: failed to persist project runtime context for {}: {}", project_name, e)
    _save_triage_state(triage_dir, state)


def _load_ones_artifacts_from_triage_state(triage_dir: Path | None) -> dict[str, Any] | None:
    state = _load_triage_state(triage_dir)
    if not state:
        return None
    ones_context = state.get("ones_context") or {}
    task_json = Path(str(ones_context.get("task_json") or "").strip()) if ones_context.get("task_json") else None
    if task_json and task_json.exists():
        try:
            payload = json.loads(task_json.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, ValueError):
            payload = None
        if isinstance(payload, dict):
            return payload

    task_ref = str(ones_context.get("task_ref") or "").strip()
    if task_ref:
        return _find_existing_ones_task_artifacts(task_ref)
    return None


def _should_capture_process_trace(msg: dict, session: dict | None, parsed: dict) -> bool:
    if session and (
        bool(session.get("analysis_mode"))
        or str(session.get("analysis_workspace") or "").strip()
    ):
        return True
    if _analysis_requested(msg):
        return True
    if _review_requested(msg):
        return True
    if _ones_link_detected(msg):
        return True
    return (parsed.get("classified_type") or "") in {"urgent_issue", "task_request"}


def _extract_rollout_tool_call_detail(name: str, arguments: Any) -> str:
    raw = str(arguments or "").strip()
    if not raw:
        return ""
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return _truncate_text(raw, limit=600)

    if isinstance(parsed, dict):
        if name == "shell_command":
            return _truncate_text(parsed.get("command", ""), limit=600)
        return _truncate_text(json.dumps(parsed, ensure_ascii=False), limit=600)
    return _truncate_text(parsed, limit=600)


def _extract_rollout_tool_output_detail(output: Any) -> str:
    text = str(output or "").strip()
    if not text:
        return ""
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    preview = "\n".join(lines[:8])
    return _truncate_text(preview, limit=1000)


def _summarize_codex_rollout(
    rollout_path: Path | None,
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    call_names: dict[str, str] = {}
    steps: list[dict[str, Any]] = []

    for event in events:
        timestamp = event.get("timestamp")
        event_type = event.get("type")
        payload = event.get("payload") or {}

        if event_type == "event_msg" and payload.get("type") == "agent_message":
            message = str(payload.get("message") or "").strip()
            if message:
                steps.append({
                    "timestamp": timestamp,
                    "kind": "commentary",
                    "title": str(payload.get("phase") or "agent update"),
                    "detail": _truncate_text(message, limit=1600),
                })
            continue

        if event_type != "response_item":
            continue

        payload_type = payload.get("type")
        if payload_type == "function_call":
            call_id = str(payload.get("call_id") or "")
            name = str(payload.get("name") or "tool")
            if call_id:
                call_names[call_id] = name
            steps.append({
                "timestamp": timestamp,
                "kind": "tool_call",
                "title": name,
                "detail": _extract_rollout_tool_call_detail(name, payload.get("arguments")),
            })
            continue

        if payload_type == "function_call_output":
            call_id = str(payload.get("call_id") or "")
            name = call_names.get(call_id, "tool")
            detail = _extract_rollout_tool_output_detail(payload.get("output"))
            if detail:
                steps.append({
                    "timestamp": timestamp,
                    "kind": "tool_output",
                    "title": f"{name} output",
                    "detail": detail,
                })
            continue

        if payload_type == "custom_tool_call":
            name = str(payload.get("name") or "custom_tool")
            steps.append({
                "timestamp": timestamp,
                "kind": "tool_call",
                "title": name,
                "detail": _truncate_text(payload.get("status") or "", limit=200),
            })

    for index, step in enumerate(steps, start=1):
        step["index"] = index

    return {
        "runtime": "codex",
        "rollout_path": str(rollout_path) if rollout_path else "",
        "steps": steps[:80],
    }


def _render_analysis_trace_markdown(
    *,
    routing_decision: dict[str, Any],
    final_decision: dict[str, Any],
    analysis_trace: dict[str, Any] | None,
) -> str:
    lines = [
        "# Analysis Process",
        "",
        f"- Route mode: `{routing_decision.get('route_mode', 'unknown')}`",
        f"- ONES link detected: `{'yes' if routing_decision.get('ones_link_detected') else 'no'}`",
        f"- Pre-project: `{routing_decision.get('pre_project') or '-'}`",
        f"- Final project: `{final_decision.get('project_name') or '-'}`",
        f"- Action: `{final_decision.get('action') or '-'}`",
        f"- Classified type: `{final_decision.get('classified_type') or '-'}`",
    ]

    rollout_path = ""
    if analysis_trace:
        rollout_path = str(analysis_trace.get("rollout_path") or "")
    if rollout_path:
        lines.append(f"- Rollout: `{rollout_path}`")

    final_reason = str(final_decision.get("reason") or "").strip()
    if final_reason:
        lines.extend(["", "## Final Reason", "", final_reason])

    if analysis_trace and analysis_trace.get("steps"):
        lines.extend(["", "## Steps", ""])
        for step in analysis_trace["steps"]:
            title = str(step.get("title") or step.get("kind") or "step")
            timestamp = str(step.get("timestamp") or "").strip()
            lines.append(f"{step.get('index', '?')}. {title}{f' ({timestamp})' if timestamp else ''}")
            detail = str(step.get("detail") or "").strip()
            if detail:
                lines.append("")
                lines.append("```text")
                lines.append(detail)
                lines.append("```")
                lines.append("")

    return "\n".join(lines)


def _extract_rollout_trace(agent_runtime: str, agent_session_id: str | None) -> dict[str, Any] | None:
    if agent_runtime != "codex" or not agent_session_id:
        return None
    try:
        from core.orchestrator.agent_client import agent_client

        rollout_path, events = agent_client._read_codex_rollout(agent_session_id)
    except Exception as e:
        logger.warning("Pipeline: failed to read codex rollout for {}: {}", agent_session_id, e)
        return None
    if not rollout_path:
        return None
    return _summarize_codex_rollout(rollout_path, events)


def _update_triage_state_after_process(
    state_path: Path,
    *,
    project: str,
) -> None:
    if not state_path.exists():
        return
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, ValueError):
        return
    if project:
        state["project"] = project
    state["updated_at"] = datetime.now().isoformat()
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_process_trace_artifacts(
    triage_dir: Path,
    *,
    message_id: int,
    session_id: int | None,
    msg: dict,
    session: dict | None,
    prompt: str,
    agent_runtime: str,
    pre_project: str,
    route_mode: str,
    direct_project: dict[str, Any] | None,
    ones_artifacts: dict[str, Any] | None,
    parsed: dict[str, Any],
    result_text: str,
    new_agent_sid: str | None,
    effective_agent_sid: str | None,
    analysis_trace: dict[str, Any] | None,
) -> None:
    process_dir = triage_dir / "02-process"
    process_dir.mkdir(parents=True, exist_ok=True)

    routing_decision = {
        "message_id": message_id,
        "session_id": session_id,
        "platform_message_id": msg.get("platform_message_id", ""),
        "problem_summary": ((session or {}).get("title") or msg.get("content") or "")[:200],
        "agent_runtime": agent_runtime,
        "pre_project": pre_project,
        "route_mode": route_mode,
        "ones_link_detected": _ones_link_detected(msg),
        "ones_artifacts": {
            "task_dir": str(((ones_artifacts or {}).get("paths") or {}).get("task_dir") or ""),
            "report_md": str(((ones_artifacts or {}).get("paths") or {}).get("report_md") or ""),
            "downloaded_files": _collect_ones_downloaded_files(ones_artifacts),
        } if ones_artifacts else None,
        "direct_project": direct_project or None,
        "effective_agent_session_id": effective_agent_sid or new_agent_sid or "",
        "rollout_path": (analysis_trace or {}).get("rollout_path", ""),
        "prompt_excerpt": _truncate_text(prompt, limit=2000),
        "created_at": datetime.now().isoformat(),
    }

    reply_content = parsed.get("reply_content")
    final_decision: dict[str, Any] = {
        "message_id": message_id,
        "session_id": session_id,
        "action": parsed.get("action", ""),
        "classified_type": parsed.get("classified_type", ""),
        "topic": parsed.get("topic", ""),
        "project_name": parsed.get("project_name", ""),
        "reason": parsed.get("reason", ""),
        "agent_runtime": agent_runtime,
        "result_session_id": new_agent_sid or "",
        "effective_agent_session_id": effective_agent_sid or "",
        "rollout_path": (analysis_trace or {}).get("rollout_path", ""),
        "reply_preview": _truncate_text(_reply_content_to_text(reply_content), limit=2000),
        "result_text_preview": _truncate_text(result_text, limit=2000),
        "created_at": datetime.now().isoformat(),
    }
    if isinstance(reply_content, dict):
        final_decision["reply_content"] = reply_content

    _write_json_artifact(process_dir / "routing_decision.json", routing_decision)
    _write_json_artifact(process_dir / "final_decision.json", final_decision)
    if analysis_trace:
        _write_json_artifact(process_dir / "analysis_trace.json", analysis_trace)
    _write_text_artifact(
        process_dir / "analysis_trace.md",
        _render_analysis_trace_markdown(
            routing_decision=routing_decision,
            final_decision=final_decision,
            analysis_trace=analysis_trace,
        ),
    )
    _update_triage_state_after_process(
        triage_dir / "00-state.json",
        project=str(final_decision.get("project_name") or pre_project or ""),
    )


def _sanitize_filename(name: str, fallback: str) -> str:
    candidate = (name or "").strip().replace("\\", "_").replace("/", "_")
    candidate = "".join(ch for ch in candidate if ch not in '<>:"|?*').strip(" .")
    return candidate or fallback


def _guess_attachment_suffix(file_name: str, media_type: str) -> str:
    suffix = Path(file_name).suffix
    if suffix:
        return suffix
    if media_type == "image":
        return ".png"
    if media_type == "audio":
        return ".audio"
    if media_type == "video":
        return ".video"
    return ".bin"


async def _download_message_media_to_workspace(msg: dict, triage_dir: Path) -> dict[str, Any]:
    media_info = _parse_media_info(msg)
    media_type = str(media_info.get("type") or "").strip()
    if media_type not in {"image", "file", "audio", "video", "post"}:
        return media_info

    if media_type == "post":
        image_keys = [str(item).strip() for item in (media_info.get("image_keys") or []) if str(item).strip()]
        if not image_keys:
            return media_info

        attachments_root = triage_dir / "01-intake" / "attachments"
        target_dir = attachments_root / str(msg["platform_message_id"])
        target_dir.mkdir(parents=True, exist_ok=True)
        original_dir = target_dir / "original"
        original_dir.mkdir(parents=True, exist_ok=True)

        from core.connectors.feishu import FeishuClient

        client = FeishuClient()
        local_paths: list[str] = []
        for index, image_key in enumerate(image_keys, start=1):
            payload = client.get_image_bytes(
                image_key,
                message_id=str(msg.get("platform_message_id") or ""),
                resource_type="image",
            )
            if not payload:
                continue
            data, downloaded_name = payload
            base_name = _sanitize_filename(downloaded_name or f"post-image-{index}", fallback=f"post-image-{index}.png")
            suffix = _guess_attachment_suffix(base_name, "image")
            if not Path(base_name).suffix:
                base_name = f"{base_name}{suffix}"
            target_file = original_dir / base_name
            target_file.write_bytes(data)
            local_paths.append(str(target_file.resolve()))

        if local_paths:
            media_info.update({
                "download_status": "downloaded",
                "local_paths": local_paths,
                "local_path": local_paths[0],
                "mime_type": "image/png",
                "proxy_url": f"/api/messages/{msg['id']}/attachment",
            })
        else:
            media_info["download_status"] = "failed"

        manifest_path = target_dir / "manifest.json"
        manifest_path.write_text(
            json.dumps({
                "message_id": msg["id"],
                "platform_message_id": msg["platform_message_id"],
                "message_type": msg["message_type"],
                "media_info": media_info,
            }, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return media_info

    attachments_root = triage_dir / "01-intake" / "attachments"
    target_dir = attachments_root / str(msg["platform_message_id"])
    target_dir.mkdir(parents=True, exist_ok=True)
    original_dir = target_dir / "original"
    original_dir.mkdir(parents=True, exist_ok=True)

    local_path_value = str(media_info.get("local_path") or msg.get("attachment_path") or "").strip()
    local_path = Path(local_path_value) if local_path_value else None
    if local_path and local_path.exists():
        data = local_path.read_bytes()
        downloaded_name = local_path.name
    else:
        remote_key = (
            media_info.get("image_key")
            or media_info.get("file_key")
            or media_info.get("video_key")
            or ""
        )
        if not remote_key:
            return media_info

        from core.connectors.feishu import FeishuClient

        client = FeishuClient()
        payload: tuple[bytes, str | None] | None = None
        if media_type == "image":
            payload = client.get_image_bytes(str(remote_key))
        else:
            payload = client.get_file_bytes(
                str(remote_key),
                message_id=str(msg.get("platform_message_id") or ""),
                resource_type=media_type,
            )

        if not payload:
            media_info["download_status"] = "failed"
            return media_info

        data, downloaded_name = payload

    base_name = _sanitize_filename(
        downloaded_name or str(media_info.get("file_name") or ""),
        fallback=f"{media_type}_{msg['platform_message_id']}",
    )
    suffix = _guess_attachment_suffix(base_name, media_type)
    if not Path(base_name).suffix:
        base_name = f"{base_name}{suffix}"

    target_file = original_dir / base_name
    target_file.write_bytes(data)
    mime_type = mimetypes.guess_type(target_file.name)[0] or (
        "image/png" if media_type == "image" else "application/octet-stream"
    )

    media_info.update({
        "download_status": "downloaded",
        "local_path": str(target_file.resolve()),
        "size_bytes": len(data),
        "mime_type": mime_type,
        "proxy_url": f"/api/messages/{msg['id']}/attachment",
    })

    manifest_path = target_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps({
            "message_id": msg["id"],
            "platform_message_id": msg["platform_message_id"],
            "message_type": msg["message_type"],
            "media_info": media_info,
        }, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return media_info


async def _persist_message_media_state(message_id: int, media_info: dict[str, Any]) -> None:
    await _update_message(
        message_id,
        media_info_json=json.dumps(media_info, ensure_ascii=False),
        attachment_path=str(media_info.get("local_path") or ""),
    )


async def _materialize_analysis_workspace(message_id: int, msg: dict, session: dict | None, session_id: int) -> Path:
    triage_dir = _analysis_dir_for_session(session_id, session, msg)
    intake_dir = triage_dir / "01-intake"
    attachments_dir = intake_dir / "attachments"
    messages_dir = intake_dir / "messages"
    attachments_dir.mkdir(parents=True, exist_ok=True)
    messages_dir.mkdir(parents=True, exist_ok=True)
    (triage_dir / "search-runs").mkdir(parents=True, exist_ok=True)

    state_path = triage_dir / "00-state.json"
    if not state_path.exists():
        state_path.write_text(
            json.dumps(_triage_state_payload(session_id, session, msg, triage_dir), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    await _bind_session_analysis_workspace(session_id, triage_dir)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY created_at ASC, id ASC",
            (session_id,),
        )
        rows = [dict(row) for row in await cursor.fetchall()]

    for row in rows:
        media_info = _parse_media_info(row)
        if media_info.get("type") in {"image", "file", "audio", "video"}:
            media_info = await _download_message_media_to_workspace(row, triage_dir)
            await _persist_message_media_state(row["id"], media_info)
            row["media_info_json"] = json.dumps(media_info, ensure_ascii=False)
            row["attachment_path"] = str(media_info.get("local_path") or "")

        snapshot_path = messages_dir / f"{row['id']}.json"
        snapshot = {
            "id": row["id"],
            "platform_message_id": row.get("platform_message_id", ""),
            "message_type": row.get("message_type", ""),
            "content": row.get("content", ""),
            "thread_id": row.get("thread_id", ""),
            "created_at": row.get("created_at", ""),
            "media_info": _parse_media_info(row),
            "attachment_path": row.get("attachment_path", ""),
            "raw_payload": row.get("raw_payload", ""),
        }
        snapshot_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    return triage_dir


def _extract_media(msg: dict) -> str:
    if msg.get("message_type") in ("text", None, ""):
        return ""
    content = msg.get("content", "")
    label = content[1:-1] if content.startswith("[") else (msg.get("message_type", "") + "消息")
    return f"用户发送了{label}"


# ---------------------------------------------------------------------------
# Session routing
# ---------------------------------------------------------------------------

async def _route_session(msg: dict) -> int | None:
    """Match message to session by thread_id or create new."""
    if not msg.get("chat_id"):
        return None

    async with aiosqlite.connect(DB_PATH) as db:
        # Match by thread_id
        if msg.get("thread_id"):
            cursor = await db.execute(
                "SELECT id FROM sessions WHERE thread_id = ? AND status IN ('open','waiting') "
                "ORDER BY last_active_at DESC LIMIT 1",
                (msg["thread_id"],),
            )
            row = await cursor.fetchone()
            if row:
                return row[0]

        # Create new session
        now = datetime.now().isoformat()
        agent_runtime = _get_default_agent_runtime()
        key = f"feishu_{msg['chat_id'][:16]}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        title = (msg.get("content") or "新会话")[:64]
        cursor = await db.execute(
            "INSERT INTO sessions (session_key, source_platform, source_chat_id, owner_user_id, "
            "title, topic, project, priority, status, thread_id, agent_runtime, summary_path, "
            "last_active_at, message_count, risk_level, needs_manual_review, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (key, "feishu", msg["chat_id"], msg.get("sender_id", ""),
             title, "", "", "normal", "open", msg.get("thread_id", ""), agent_runtime, "",
             now, 0, "low", False, now, now),
        )
        await db.commit()
        return cursor.lastrowid


async def _attach_message_to_session(message_id: int, session_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        # Update session counters
        now = datetime.now().isoformat()
        await db.execute(
            "UPDATE sessions SET message_count = message_count + 1, "
            "last_active_at = ?, updated_at = ? WHERE id = ?",
            (now, now, session_id),
        )
        # Add session_message link
        cursor = await db.execute(
            "SELECT COALESCE(MAX(sequence_no), 0) FROM session_messages WHERE session_id = ?",
            (session_id,),
        )
        max_seq = (await cursor.fetchone())[0]
        await db.execute(
            "INSERT INTO session_messages (session_id, message_id, role, sequence_no, created_at) "
            "VALUES (?,?,?,?,?)",
            (session_id, message_id, "user", max_seq + 1, now),
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Reply delivery (feishu + DB)
# ---------------------------------------------------------------------------

async def _deliver_reply(
    msg: dict,
    content: Any,
    session_id: int | None,
    *,
    classified_type: str | None = None,
    topic: str | None = None,
    reply_model: str | None = None,
    reply_usage: dict[str, Any] | None = None,
    project_context: Any = None,
) -> tuple[str | None, bool]:
    """Send reply to feishu and save to DB. Returns (thread_id, delivered)."""
    delivered = False
    thread_id = ""
    msg_type = "text"
    db_content = _reply_content_to_text(content)
    raw_payload = ""
    try:
        from core.connectors.feishu import FeishuClient
        client = FeishuClient()
        msg_type, body_content, db_content, presentation = _prepare_feishu_reply(
            content,
            client,
            classified_type=classified_type,
            topic=topic,
            model_id=reply_model,
            usage=reply_usage,
            project_context=project_context,
        )
        raw_payload = _safe_json_dumps({
            "msg_type": msg_type,
            "content": body_content,
            "db_content": db_content,
            "presentation": presentation,
        })
        result = client.reply_message(
            message_id=msg["platform_message_id"],
            content=body_content,
            msg_type=msg_type,
            reply_in_thread=True,
        )
        thread_id = result.get("thread_id", "") if result else ""
        delivered = result is not None
        if delivered:
            logger.info("Pipeline: delivered reply for message {}", msg["id"])
    except Exception as e:
        logger.warning("Pipeline: feishu reply failed for message {}: {}", msg["id"], e)

    # Save bot reply to DB
    try:
        now = datetime.now().isoformat()
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT OR IGNORE INTO messages (platform, platform_message_id, chat_id, "
                "sender_id, sender_name, message_type, content, received_at, raw_payload, "
                "thread_id, root_id, parent_id, classified_type, "
                "session_id, pipeline_status, pipeline_error, processed_at, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (msg.get("platform", "feishu"), f"reply_{msg['platform_message_id']}",
                 msg["chat_id"], "bot", "WorkAgent", msg_type, db_content[:4000],
                 now, raw_payload,
                 thread_id or "", "", "", "bot_reply",
                 session_id, "completed", "", now, now),
            )
            if session_id:
                cursor = await db.execute(
                    "SELECT id FROM messages WHERE platform_message_id = ?",
                    (f"reply_{msg['platform_message_id']}",),
                )
                row = await cursor.fetchone()
                reply_id = row[0] if row else None
                if reply_id:
                    cursor = await db.execute(
                        "SELECT 1 FROM session_messages WHERE session_id = ? AND message_id = ?",
                        (session_id, reply_id),
                    )
                    exists = await cursor.fetchone()
                    if not exists:
                        cursor = await db.execute(
                            "SELECT COALESCE(MAX(sequence_no), 0) FROM session_messages WHERE session_id = ?",
                            (session_id,),
                        )
                        max_seq = (await cursor.fetchone())[0]
                        await db.execute(
                            "INSERT INTO session_messages (session_id, message_id, role, sequence_no, created_at) "
                            "VALUES (?,?,?,?,?)",
                            (session_id, reply_id, "assistant", max_seq + 1, now),
                        )
            await db.commit()
    except Exception as e:
        logger.warning("Pipeline: save bot reply failed: {}", e)

    return thread_id or None, delivered


# ---------------------------------------------------------------------------
# Session state persistence
# ---------------------------------------------------------------------------

async def _update_session_state(session_id: int, *,
                                 agent_session_id: str | None = None,
                                 agent_runtime: str | None = None,
                                 project: str = "",
                                 thread_id: str | None = None) -> None:
    """Write agent session metadata to session (only if not already set)."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT agent_session_id, agent_runtime, project, thread_id FROM sessions WHERE id = ?",
            (session_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return

        current_sid, current_runtime, current_project, current_thread = row
        updates, params = [], []

        if not current_sid and agent_session_id:
            updates.append("agent_session_id = ?")
            params.append(agent_session_id)

        if (not current_runtime or current_runtime == DEFAULT_AGENT_RUNTIME) and agent_runtime:
            updates.append("agent_runtime = ?")
            params.append(normalize_agent_runtime(agent_runtime))

        if not current_project and project:
            updates.append("project = ?")
            params.append(project)

        if not current_thread and thread_id:
            updates.append("thread_id = ?")
            params.append(thread_id)
            logger.info("Session {} bound to thread_id {}", session_id, thread_id)

        if updates:
            updates.append("updated_at = ?")
            params.append(datetime.now().isoformat())
            params.append(session_id)
            await db.execute(
                f"UPDATE sessions SET {', '.join(updates)} WHERE id = ?", params,
            )
            await db.commit()


# ---------------------------------------------------------------------------
# DB helpers (all raw SQL)
# ---------------------------------------------------------------------------

async def _read_message(message_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM messages WHERE id = ?", (message_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def _update_message(message_id: int, **fields) -> None:
    if not fields:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        await db.execute(
            f"UPDATE messages SET {set_clause} WHERE id = ?",
            [*fields.values(), message_id],
        )
        await db.commit()


async def _read_session(session_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def _audit(event_type: str, target_type: str, target_id: str,
                 detail: dict | str) -> None:
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO audit_logs (event_type, target_type, target_id, detail, "
                "operator, created_at) VALUES (?,?,?,?,?,?)",
                (event_type, target_type, target_id,
                 json.dumps(detail, ensure_ascii=False) if isinstance(detail, dict) else detail,
                 "orchestrator", datetime.now().isoformat()),
            )
            await db.commit()
    except Exception as e:
        logger.warning("Audit log failed: {}", e)


async def _record_agent_run(message_id: int, session_id: int | None,
                             agent_session_id: str | None,
                             agent_runtime: str,
                             result: dict) -> None:
    try:
        now = datetime.now().isoformat()
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO agent_runs (message_id, agent_name, runtime_type, "
                "session_id, status, started_at, ended_at, cost_usd, "
                "input_path, output_path, input_tokens, output_tokens, error_message) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (message_id, "orchestrator", get_agent_run_runtime_type(agent_runtime), session_id, "success",
                 now, now, result.get("cost_usd", 0),
                 f"agent_session:{agent_session_id}" if agent_session_id else "",
                 result.get("text", "")[:2000],
                 result.get("num_turns", 0), 0, ""),
            )
            await db.commit()
    except Exception as e:
        logger.warning("Record agent run failed: {}", e)


# ---------------------------------------------------------------------------
# JSON parsing
# ---------------------------------------------------------------------------

def _parse_result(text: str) -> dict:
    """Extract JSON with 'action' key from agent output."""
    text = text.strip()
    if not text:
        return _fallback("empty output")

    def _valid(d):
        return isinstance(d, dict) and "action" in d

    # Try: whole text
    try:
        d = json.loads(text)
        if _valid(d):
            return d
    except (json.JSONDecodeError, ValueError):
        pass

    # Try: fenced ```json blocks
    if "```" in text:
        for block in text.split("```"):
            block = block.strip()
            if block.startswith("json"):
                block = block[4:].strip()
            try:
                d = json.loads(block)
                if _valid(d):
                    return d
            except (json.JSONDecodeError, ValueError):
                continue

    # Try: last balanced { } containing "action"
    pos = len(text)
    while pos > 0:
        end = text.rfind("}", 0, pos)
        if end == -1:
            break
        depth, start = 0, end
        while start >= 0:
            if text[start] == "}":
                depth += 1
            elif text[start] == "{":
                depth -= 1
                if depth == 0:
                    break
            start -= 1
        if start >= 0 and depth == 0:
            try:
                d = json.loads(text[start:end + 1])
                if _valid(d):
                    return d
            except (json.JSONDecodeError, ValueError):
                pass
        pos = start

    # Parse failed but agent produced text — use it directly as reply
    if text:
        logger.warning("Pipeline: JSON parse failed, using raw text as reply (len={})", len(text))
        return {
            "action": "replied",
            "classified_type": "chat",
            "topic": "",
            "project_name": None,
            "reply_content": text,
            "reason": "parse_failed_fallback",
        }

    return _fallback("failed to parse")


def _normalize_project_result(
    text: str,
    *,
    session: dict | None,
    project: str,
) -> dict:
    structured_reply = _extract_structured_reply_payload(text)
    if structured_reply:
        return {
            "action": "replied",
            "classified_type": "work_question",
            "topic": (session or {}).get("title") or "",
            "project_name": project,
            "reply_content": structured_reply,
            "reason": "structured project reply",
        }

    parsed = _parse_result(text)
    reply_content = parsed.get("reply_content") or text
    action = parsed.get("action") or "replied"
    if action not in {"replied", "drafted", "silent"}:
        action = "replied"

    classified_type = parsed.get("classified_type")
    if (
        classified_type in {None, "chat"}
        and parsed.get("reason") == "parse_failed_fallback"
    ):
        classified_type = "work_question"

    return {
        "action": action,
        "classified_type": classified_type or "work_question",
        "topic": parsed.get("topic") or ((session or {}).get("title") or ""),
        "project_name": parsed.get("project_name") or project,
        "reply_content": reply_content,
        "reason": parsed.get("reason") or "resumed project session",
    }


def _should_retry_project_response(parsed: dict) -> bool:
    action = parsed.get("action") or "replied"
    if action == "silent":
        return False

    reply = _reply_content_to_text(parsed.get("reply_content")).strip()
    if not reply or len(reply) > 800:
        return False

    indicators = [
        "我先确认",
        "先确认一下",
        "你是想",
        "你是指",
        "请先说明",
        "方便补充",
        "需要你确认",
        "能否补充",
        "请补充",
        "还是说",
    ]
    evidence_markers = [
        "我检查了",
        "我搜索了",
        "根据代码",
        "根据当前代码",
        "在代码里",
        "从代码看",
        "搜索到",
        "文件",
        "`",
        "#L",
    ]

    has_indicator = any(marker in reply for marker in indicators) or reply.endswith(("?", "？"))
    has_evidence = any(marker in reply for marker in evidence_markers)
    return has_indicator and not has_evidence


def _fallback(reason: str) -> dict:
    return {"action": "replied", "classified_type": "chat", "topic": "",
            "reply_content": "", "reason": reason}
