"""Skill registry and manifest helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.config import settings


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def discover_skill_registry(skills_root: Path | None = None) -> dict[str, Any]:
    root = skills_root or (settings.project_root / ".claude" / "skills")
    skills: list[dict[str, Any]] = []
    if not root.exists():
        return {"skills": skills}

    for skill_file in sorted(root.glob("*/SKILL.md")):
        if not skill_file.is_file():
            continue
        name = skill_file.parent.name
        frontmatter, body = _split_frontmatter(skill_file.read_text(encoding="utf-8"))
        description = str(frontmatter.get("description") or "").strip()
        trigger_summary = _extract_trigger_summary(body) or description
        skills.append({
            "name": str(frontmatter.get("name") or name),
            "path": _relative_or_absolute(skill_file),
            "trigger_summary": trigger_summary,
            "scripts_dir": _relative_or_absolute(skill_file.parent / "scripts"),
            "schemas_dir": _relative_or_absolute(skill_file.parent / "schemas"),
        })
    return {"skills": skills}


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    meta: dict[str, str] = {}
    for line in parts[1].splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        meta[key.strip()] = value.strip().strip("'\"")
    return meta, parts[2]


def _extract_trigger_summary(body: str) -> str:
    lines = body.splitlines()
    for index, line in enumerate(lines):
        if line.strip().lower() == "## trigger":
            collected: list[str] = []
            for item in lines[index + 1 :]:
                stripped = item.strip()
                if stripped.startswith("## "):
                    break
                if stripped.startswith("- "):
                    collected.append(stripped[2:].strip())
                elif stripped and not stripped.startswith("#"):
                    collected.append(stripped)
                if len(" ".join(collected)) > 240:
                    break
            return " ".join(collected)[:300].strip()
    return ""


def _relative_or_absolute(path: Path) -> str:
    try:
        return str(path.relative_to(settings.project_root))
    except ValueError:
        return str(path)
