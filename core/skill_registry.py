"""Skill registry discovery for .claude skills."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from claude_agent_sdk import AgentDefinition

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_AGENTS_DIR = PROJECT_ROOT / ".claude" / "agents"
DEFAULT_SKILLS_DIR = PROJECT_ROOT / ".claude" / "skills"


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---"):
        return {}, text

    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text

    meta: dict[str, Any] = {}
    current_key = None

    for line in parts[1].strip().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        if stripped.startswith("- ") and current_key and isinstance(meta.get(current_key), list):
            meta[current_key].append(stripped[2:].strip())
            continue

        if ":" not in stripped:
            continue
        key, val = stripped.split(":", 1)
        key = key.strip()
        val = val.strip()
        current_key = key

        if val == "":
            meta[key] = []
        elif val.isdigit():
            meta[key] = int(val)
        elif val in ("true", "false"):
            meta[key] = val == "true"
        else:
            meta[key] = val

    return meta, parts[2].strip()


def _load_from_md(md_file: Path) -> tuple[str, AgentDefinition, str] | None:
    try:
        text = md_file.read_text(encoding="utf-8")
    except OSError:
        return None

    meta, body = _parse_frontmatter(text)
    if not body:
        return None

    name = meta.get("name", md_file.stem)
    description = meta.get("description", "")
    tools = meta.get("tools") if isinstance(meta.get("tools"), list) else None
    max_turns = meta.get("maxTurns")
    model = meta.get("model")

    definition = AgentDefinition(
        description=description,
        prompt=body,
        tools=tools,
        maxTurns=max_turns,
        model=model,
    )
    return name, definition, description


def discover_skills(
    agents_dir: Path | None = None,
    skills_dir: Path | None = None,
) -> tuple[dict[str, AgentDefinition], dict[str, str]]:
    """Discover agent definitions and workflow skills."""

    target_agents_dir = agents_dir or DEFAULT_AGENTS_DIR
    target_skills_dir = DEFAULT_SKILLS_DIR if skills_dir is None else skills_dir
    registry: dict[str, AgentDefinition] = {}
    descriptions: dict[str, str] = {}

    if target_agents_dir.exists():
        for md_file in sorted(target_agents_dir.glob("*.md")):
            if not md_file.is_file():
                continue
            result = _load_from_md(md_file)
            if result:
                name, definition, description = result
                registry[name] = definition
                descriptions[name] = description

    if target_skills_dir and target_skills_dir.exists():
        for md_file in sorted(target_skills_dir.glob("*/SKILL.md")):
            if not md_file.is_file():
                continue
            result = _load_from_md(md_file)
            if result:
                name, definition, description = result
                registry[name] = definition
                descriptions[name] = description

    return registry, descriptions


SKILL_REGISTRY, SKILL_DESCRIPTIONS = discover_skills()
