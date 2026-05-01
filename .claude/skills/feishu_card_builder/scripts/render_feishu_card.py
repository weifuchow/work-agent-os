#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from typing import Any


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
    normalized = {
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
        if content:
            elements.append(markdown(content))

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
        if title or content:
            sections.append({"title": title, "content": content})
    return sections


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
