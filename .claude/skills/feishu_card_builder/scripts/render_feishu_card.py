#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any


CODE_BLOCK_RE = re.compile(
    r"```(?P<info>[^\n`]*)\n(?P<code>.*?)```",
    re.DOTALL,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Render a structured summary reply contract.")
    parser.add_argument("--input", help="JSON input file. Reads stdin when omitted.")
    parser.add_argument(
        "--type",
        choices=["markdown", "feishu_card", "summary_json"],
        default="markdown",
        help="Output format. summary_json prints the normalized summary object only.",
    )
    args = parser.parse_args()

    if args.input:
        source = open(args.input, encoding="utf-8-sig").read()
    else:
        source = sys.stdin.read()
    source = source.lstrip("\ufeff")
    payload = json.loads(source or "{}")
    if args.type == "summary_json":
        result = render_structured_summary(payload)
    else:
        result = render_contract(payload, reply_type=args.type)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def render_contract(payload: dict[str, Any], reply_type: str = "markdown") -> dict[str, Any]:
    if reply_type == "markdown":
        return render_markdown_contract(payload)
    if reply_type != "feishu_card":
        raise ValueError(f"unsupported reply_type: {reply_type}")
    return render_feishu_card_contract(payload)


def render_markdown_contract(payload: dict[str, Any]) -> dict[str, Any]:
    summary = render_structured_summary(payload)
    content = render_markdown_summary(summary)
    return {
        "action": "reply",
        "reply": {
            "channel": "feishu",
            "type": "markdown",
            "content": content,
            "payload": {"structured_summary": summary},
        },
        "session_patch": {},
        "workspace_patch": {},
        "skill_trace": [
            {"skill": "feishu_card_builder", "reason": "rendered structured summary"}
        ],
        "audit": [],
    }


def render_feishu_card_contract(payload: dict[str, Any]) -> dict[str, Any]:
    fallback = fallback_text(payload)
    return {
        "action": "reply",
        "reply": {
            "channel": "feishu",
            "type": "feishu_card",
            "content": fallback,
            "payload": render_card(payload),
        },
        "session_patch": {},
        "workspace_patch": {},
        "skill_trace": [
            {"skill": "feishu_card_builder", "reason": "rendered feishu_card payload"}
        ],
        "audit": [],
    }


def render_structured_summary(payload: dict[str, Any]) -> dict[str, Any]:
    title = str(payload.get("title") or "工作摘要").strip()
    summary = str(payload.get("summary") or payload.get("conclusion") or "").strip()
    conclusion = str(payload.get("conclusion") or summary).strip()
    confidence = normalize_confidence(payload.get("confidence"))
    sections = normalize_sections(payload.get("sections"))
    table = payload.get("table") if isinstance(payload.get("table"), dict) else None
    code_blocks = normalize_code_blocks(payload.get("code_blocks"))
    flowcharts = normalize_flowcharts(payload)
    steps = normalize_steps(payload.get("steps"))
    normalized = {
        "format": str(payload.get("format") or "").strip(),
        "title": title,
        "summary": summary,
        "conclusion": conclusion,
        "confidence": confidence,
        "facts": string_list(payload.get("facts")),
        "evidence": string_list(payload.get("evidence")),
        "risks": string_list(payload.get("risks")),
        "missing_items": string_list(payload.get("missing_items")),
        "next_steps": string_list(payload.get("next_steps")),
        "sections": sections,
        "code_blocks": code_blocks,
        "flowcharts": flowcharts,
        "steps": steps,
        "table": table,
        "fallback_text": fallback_text(payload),
    }
    if not normalized["summary"]:
        normalized["summary"] = first_non_empty(
            normalized["conclusion"],
            first_section_content(sections),
            normalized["fallback_text"],
            title,
        )
    if not normalized["conclusion"]:
        normalized["conclusion"] = normalized["summary"]
    return normalized


def render_markdown_summary(summary: dict[str, Any]) -> str:
    lines = [f"# {summary.get('title') or '工作摘要'}", ""]
    if summary.get("summary"):
        lines.extend([str(summary["summary"]), ""])
    if summary.get("conclusion"):
        lines.extend(["## 结论", str(summary["conclusion"]), ""])
    if summary.get("confidence"):
        lines.extend(["## 置信度", str(summary["confidence"]), ""])

    for key, title in [
        ("facts", "已确认事实"),
        ("evidence", "关键证据"),
        ("risks", "风险"),
        ("missing_items", "待补充"),
        ("next_steps", "下一步"),
    ]:
        items = summary.get(key) or []
        if items:
            lines.extend([f"## {title}", *[f"- {item}" for item in items], ""])

    for section in summary.get("sections") or []:
        section_title = str(section.get("title") or "").strip()
        content = str(section.get("content") or "").strip()
        if section_title:
            lines.append(f"## {section_title}")
        if content:
            lines.append(content)
        if section_title or content:
            lines.append("")

    flowcharts = summary.get("flowcharts") or []
    if flowcharts:
        lines.extend(["## 流程图", ""])
        for chart in flowcharts:
            title = str(chart.get("title") or "流程图").strip()
            description = str(chart.get("description") or "").strip()
            source = str(chart.get("source") or "").strip()
            if title:
                lines.append(f"### {title}")
            if description:
                lines.append(description)
            if source:
                lines.append("```mermaid")
                lines.append(source)
                lines.append("```")
            lines.append("")

    steps = summary.get("steps") or []
    if steps:
        lines.extend(["## 流程步骤", ""])
        for index, step in enumerate(steps, start=1):
            title = str(step.get("title") or f"步骤 {index}").strip()
            detail = str(step.get("detail") or "").strip()
            lines.append(f"{index}. {title}" + (f"：{detail}" if detail else ""))
        lines.append("")

    code_blocks = summary.get("code_blocks") or []
    if code_blocks:
        lines.extend(["## 代码片段", ""])
        for block in code_blocks:
            title = str(block.get("title") or block.get("path") or "代码片段").strip()
            language = str(block.get("language") or "").strip()
            path = str(block.get("path") or "").strip()
            note = str(block.get("note") or "").strip()
            code = str(block.get("code") or "").strip()
            if title:
                lines.append(f"### {title}")
            if path:
                lines.append(f"文件：`{path}`")
            if note:
                lines.append(note)
            if code:
                lines.append(f"```{language}".rstrip())
                lines.append(code)
                lines.append("```")
            lines.append("")

    table = summary.get("table")
    if isinstance(table, dict):
        table_markdown = render_markdown_table(table)
        if table_markdown:
            lines.extend([table_markdown, ""])

    return "\n".join(lines).strip()


def render_card(payload: dict[str, Any]) -> dict[str, Any]:
    structured = render_structured_summary(payload)
    title = str(structured.get("title") or "工作回复").strip()
    summary = str(structured.get("summary") or "").strip()
    elements: list[dict[str, Any]] = []

    if summary:
        elements.append(markdown(summary))

    conclusion = str(structured.get("conclusion") or "").strip()
    if conclusion and conclusion != summary:
        elements.append(markdown("**结论**"))
        elements.append(markdown(conclusion))

    for key, title_text in [
        ("facts", "已确认事实"),
        ("evidence", "关键证据"),
        ("risks", "风险"),
        ("missing_items", "待补充"),
        ("next_steps", "下一步"),
    ]:
        items = structured.get(key) or []
        if items:
            elements.append(markdown(f"**{title_text}**"))
            elements.append(markdown("\n".join(f"- {item}" for item in items)))

    for section in structured.get("sections") or []:
        if not isinstance(section, dict):
            continue
        section_title = str(section.get("title") or "").strip()
        content = str(section.get("content") or "").strip()
        if section_title:
            elements.append(markdown(f"**{section_title}**"))
        section_code_blocks = code_blocks_from_section(section)
        if content:
            elements.extend(render_content_elements(content))
        if section_code_blocks:
            elements.extend(render_code_blocks(section_code_blocks))

    flowcharts = structured.get("flowcharts") or []
    if flowcharts:
        elements.extend(render_flowcharts(flowcharts))

    steps = structured.get("steps") or []
    if steps:
        elements.extend(render_steps(steps))

    code_blocks = structured.get("code_blocks") or []
    if code_blocks:
        elements.extend(render_code_blocks(code_blocks))

    table = structured.get("table")
    if isinstance(table, dict):
        table_element = render_table(table)
        if table_element:
            elements.append(table_element)

    if not elements:
        elements.append(markdown(fallback_text(payload) or title))

    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {
            "template": str(payload.get("color") or "blue"),
            "title": {"tag": "plain_text", "content": title},
        },
        "body": {"elements": elements},
    }


def normalize_confidence(value: Any) -> str:
    confidence = str(value or "").strip().lower()
    if confidence in {"high", "medium", "low"}:
        return confidence
    return "medium"


def normalize_sections(value: Any) -> list[dict[str, str]]:
    sections: list[dict[str, str]] = []
    if not isinstance(value, list):
        return sections
    for item in value:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        content = str(item.get("content") or "").strip()
        code = str(item.get("code") or "").strip()
        path = str(item.get("path") or item.get("file") or "").strip()
        language = str(item.get("language") or item.get("lang") or "").strip()
        note = str(item.get("note") or item.get("description") or "").strip()
        if title or content or code:
            section = {"title": title, "content": content}
            if code:
                section["code"] = code
            if path:
                section["path"] = path
            if language:
                section["language"] = language
            if note:
                section["note"] = note
            sections.append(section)
    return sections


def normalize_code_blocks(value: Any) -> list[dict[str, str]]:
    blocks: list[dict[str, str]] = []
    if value is None:
        return blocks
    raw_items = value if isinstance(value, list) else [value]
    for item in raw_items:
        if isinstance(item, str):
            code = item.strip()
            if code:
                blocks.append({"title": "代码片段", "path": "", "language": "", "code": code, "note": ""})
            continue
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or item.get("content") or "").strip()
        if not code:
            continue
        title = str(item.get("title") or item.get("name") or item.get("path") or "代码片段").strip()
        blocks.append(
            {
                "title": title,
                "path": str(item.get("path") or item.get("file") or "").strip(),
                "language": str(item.get("language") or item.get("lang") or "").strip(),
                "code": code,
                "note": str(item.get("note") or item.get("description") or "").strip(),
            }
        )
    return blocks


def normalize_flowcharts(payload: dict[str, Any]) -> list[dict[str, str]]:
    raw = payload.get("flowcharts")
    if raw is None and payload.get("mermaid"):
        raw = [
            {
                "title": payload.get("flow_title") or payload.get("diagram_title") or "关键流程图",
                "description": payload.get("flow_summary") or payload.get("summary") or "",
                "source": payload.get("mermaid"),
            }
        ]
    return _normalize_flowchart_items(raw)


def _normalize_flowchart_items(value: Any) -> list[dict[str, str]]:
    charts: list[dict[str, str]] = []
    if value is None:
        return charts
    raw_items = value if isinstance(value, list) else [value]
    for item in raw_items:
        if isinstance(item, str):
            source = item.strip()
            if source:
                charts.append({"title": "关键流程图", "description": "", "source": source})
            continue
        if not isinstance(item, dict):
            continue
        source = str(item.get("source") or item.get("mermaid") or item.get("code") or "").strip()
        if not source:
            continue
        charts.append(
            {
                "title": str(item.get("title") or item.get("name") or "关键流程图").strip(),
                "description": str(item.get("description") or item.get("summary") or item.get("note") or "").strip(),
                "source": source,
            }
        )
    return charts


def normalize_steps(value: Any) -> list[dict[str, str]]:
    steps: list[dict[str, str]] = []
    if not isinstance(value, list):
        return steps
    for index, item in enumerate(value, start=1):
        if isinstance(item, str):
            title = item.strip()
            if title:
                steps.append({"title": title, "detail": ""})
            continue
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or item.get("name") or item.get("step") or f"步骤 {index}").strip()
        detail = str(item.get("detail") or item.get("description") or item.get("content") or "").strip()
        if title or detail:
            steps.append({"title": title or f"步骤 {index}", "detail": detail})
    return steps


def code_blocks_from_section(section: dict[str, Any]) -> list[dict[str, str]]:
    code = str(section.get("code") or "").strip()
    if not code:
        return []
    return normalize_code_blocks(
        [
            {
                "title": section.get("title") or section.get("path") or "代码片段",
                "path": section.get("path") or "",
                "language": section.get("language") or "",
                "code": code,
                "note": section.get("note") or "",
            }
        ]
    )


def string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, list):
        return [str(value).strip()] if str(value).strip() else []
    return [str(item).strip() for item in value if str(item).strip()]


def first_non_empty(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def first_section_content(sections: list[dict[str, str]]) -> str:
    for section in sections:
        content = str(section.get("content") or "").strip()
        if content:
            return content
    return ""


def render_content_elements(content: str) -> list[dict[str, Any]]:
    text = str(content or "")
    if "```" not in text:
        return [markdown(text)] if text.strip() else []

    elements: list[dict[str, Any]] = []
    cursor = 0
    for match in CODE_BLOCK_RE.finditer(text):
        before = text[cursor:match.start()].strip()
        if before:
            elements.append(markdown(before))
        info = match.group("info").strip()
        code = match.group("code").strip()
        if code:
            language, path = parse_code_info(info)
            if language.lower() == "mermaid":
                elements.extend(render_flowcharts([{"title": "关键流程图", "description": "", "source": code}]))
            else:
                elements.extend(
                    render_code_blocks(
                        [
                            {
                                "title": path or "代码片段",
                                "path": path,
                                "language": language,
                                "code": code,
                                "note": "",
                            }
                        ]
                    )
                )
        cursor = match.end()

    tail = text[cursor:].strip()
    if tail:
        elements.append(markdown(tail))
    return elements


def parse_code_info(info: str) -> tuple[str, str]:
    raw = str(info or "").strip()
    if not raw:
        return "", ""
    language = ""
    path = ""
    parts = raw.split()
    if parts:
        language = parts[0].strip()
    for part in parts[1:]:
        if part.startswith(("path=", "file=")):
            path = part.split("=", 1)[1].strip().strip("'\"")
    if not path and len(parts) >= 2 and any(sep in parts[1] for sep in ("/", "\\", ".")):
        path = parts[1].strip()
    return language, path


def render_code_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    elements: list[dict[str, Any]] = []
    for block in blocks:
        code = str(block.get("code") or "").strip()
        if not code:
            continue
        title = str(block.get("title") or block.get("path") or "代码片段").strip()
        path = str(block.get("path") or "").strip()
        language = str(block.get("language") or "").strip()
        note = str(block.get("note") or "").strip()

        meta: list[str] = [f"**代码片段：{title}**"]
        detail: list[str] = []
        if path:
            detail.append(f"文件 `{path}`")
        if language:
            detail.append(f"语言 `{language}`")
        if detail:
            meta.append(" · ".join(detail))
        if note:
            meta.append(note)
        elements.append(markdown("\n".join(meta)))
        elements.append(markdown(render_code_fence(code, language)))
    return elements


def render_flowcharts(charts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    elements: list[dict[str, Any]] = []
    for chart in charts:
        source = str(chart.get("source") or chart.get("mermaid") or "").strip()
        if not source:
            continue
        title = str(chart.get("title") or "关键流程图").strip()
        description = str(chart.get("description") or "").strip()
        meta = [f"**流程图：{title}**"]
        if description:
            meta.append(description)
        elements.append(markdown("\n".join(meta)))
        elements.append(markdown(render_mermaid_fence(source)))
    return elements


def render_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    lines: list[str] = []
    for index, step in enumerate(steps, start=1):
        title = str(step.get("title") or f"步骤 {index}").strip()
        detail = str(step.get("detail") or "").strip()
        lines.append(f"{index}. {title}" + (f"：{detail}" if detail else ""))
    return [markdown("**流程步骤**"), markdown("\n".join(lines))] if lines else []


def render_code_fence(code: str, language: str = "") -> str:
    lang = re.sub(r"[^A-Za-z0-9_+.#-]+", "", str(language or "").strip())
    return f"```{lang}\n{str(code).strip()}\n```"


def render_mermaid_fence(source: str) -> str:
    return f"```mermaid\n{str(source).strip()}\n```"


def render_markdown_table(table: dict[str, Any]) -> str:
    rows = [row for row in table.get("rows") or [] if isinstance(row, dict)]
    columns = normalize_columns(table.get("columns") or [], rows)
    if not columns:
        return ""
    keys = [column["name"] for column in columns]
    headers = [column["display_name"] for column in columns]
    lines = [
        "| " + " | ".join(escape_markdown_cell(item) for item in headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        values = [escape_markdown_cell(row.get(key, "")) for key in keys]
        lines.append("| " + " | ".join(values) + " |")
    return "\n".join(lines)


def escape_markdown_cell(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", "<br>")


def markdown(content: str) -> dict[str, Any]:
    return {
        "tag": "markdown",
        "content": content,
        "text_align": "left",
        "text_size": "normal",
    }


def render_table(table: dict[str, Any]) -> dict[str, Any] | None:
    rows = [row for row in table.get("rows") or [] if isinstance(row, dict)]
    columns = normalize_columns(table.get("columns") or [], rows)
    if not columns:
        return None
    keys = [column["name"] for column in columns]
    return {
        "tag": "table",
        "page_size": min(max(len(rows), 1), 10),
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
        "rows": [{key: str(row.get(key, "")) for key in keys} for row in rows],
    }


def normalize_columns(raw_columns: list[Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not raw_columns and rows:
        raw_columns = list(rows[0].keys())

    columns: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_columns):
        if isinstance(raw, str):
            key = raw.strip() or f"col_{index + 1}"
            label = key
            data_type = "text"
        elif isinstance(raw, dict):
            key = str(
                raw.get("key")
                or raw.get("name")
                or raw.get("field")
                or f"col_{index + 1}"
            ).strip()
            label = str(raw.get("label") or raw.get("display_name") or key).strip()
            data_type = str(raw.get("type") or raw.get("data_type") or "text").strip()
        else:
            continue
        if not key:
            continue
        if data_type == "markdown":
            data_type = "lark_md"
        if data_type not in {"text", "lark_md", "number", "date", "options", "persons"}:
            data_type = "text"
        columns.append({"name": key, "display_name": label or key, "data_type": data_type})
    return columns


def fallback_text(payload: dict[str, Any]) -> str:
    explicit = str(payload.get("fallback_text") or "").strip()
    if explicit:
        return explicit
    parts = [
        str(payload.get("title") or "").strip(),
        str(payload.get("summary") or "").strip(),
        str(payload.get("conclusion") or "").strip(),
    ]
    for key in ("facts", "evidence", "risks", "missing_items", "next_steps"):
        values = string_list(payload.get(key))
        if values:
            parts.append("; ".join(values))
    for section in payload.get("sections") or []:
        if isinstance(section, dict):
            title = str(section.get("title") or "").strip()
            content = str(section.get("content") or "").strip()
            if title and content:
                parts.append(f"{title}: {content}")
            elif content:
                parts.append(content)
    return "\n\n".join(part for part in parts if part)


if __name__ == "__main__":
    main()
