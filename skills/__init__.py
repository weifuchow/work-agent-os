"""Skill registry — auto-discovers skills from SKILL.md files and .claude/agents/*.md."""

import re
from pathlib import Path
from typing import Any

from claude_agent_sdk import AgentDefinition

SKILLS_DIR = Path(__file__).resolve().parent
AGENTS_DIR = SKILLS_DIR.parent / ".claude" / "agents"


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Parse YAML-like frontmatter from markdown."""
    if not text.startswith("---"):
        return {}, text

    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text

    meta: dict[str, Any] = {}
    for line in parts[1].strip().splitlines():
        line = line.strip()
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        key = key.strip()
        val = val.strip()
        # Handle list values (YAML-like)
        if val == "":
            # Check for list items below
            continue
        if val.isdigit():
            meta[key] = int(val)
        elif val in ("true", "false"):
            meta[key] = val == "true"
        else:
            meta[key] = val

    # Parse list values (- item)
    current_key = None
    for line in parts[1].strip().splitlines():
        stripped = line.strip()
        if ":" in stripped and not stripped.startswith("-"):
            key, val = stripped.split(":", 1)
            current_key = key.strip()
            if val.strip() == "":
                meta[current_key] = []
        elif stripped.startswith("- ") and current_key and isinstance(meta.get(current_key), list):
            meta[current_key].append(stripped[2:].strip())

    body = parts[2].strip()
    return meta, body


def load_agent_definition(md_path: Path) -> tuple[str, AgentDefinition, dict[str, Any]]:
    """Load an AgentDefinition from a .claude/agents/*.md file."""
    text = md_path.read_text(encoding="utf-8")
    meta, body = _parse_frontmatter(text)

    name = meta.get("name", md_path.stem)
    description = meta.get("description", "")
    tools = meta.get("tools", None)
    max_turns = meta.get("maxTurns", None)

    definition = AgentDefinition(
        description=description,
        prompt=body,
        tools=tools if isinstance(tools, list) else None,
        maxTurns=max_turns,
    )

    return name, definition, meta


def load_skill_meta(skill_md_path: Path) -> dict[str, Any]:
    """Load metadata from a SKILL.md file."""
    text = skill_md_path.read_text(encoding="utf-8")
    meta, body = _parse_frontmatter(text)
    meta["body"] = body
    return meta


def discover_skills() -> tuple[dict[str, AgentDefinition], dict[str, str], dict[str, dict]]:
    """Auto-discover all skills.

    Returns:
        (SKILL_REGISTRY, SKILL_DESCRIPTIONS, SKILL_META)
    """
    registry: dict[str, AgentDefinition] = {}
    descriptions: dict[str, str] = {}
    metas: dict[str, dict] = {}

    # Load from .claude/agents/*.md
    if AGENTS_DIR.exists():
        for md_file in sorted(AGENTS_DIR.glob("*.md")):
            name, definition, meta = load_agent_definition(md_file)
            registry[name] = definition
            descriptions[name] = meta.get("description", "")
            metas[name] = meta

    # Enrich with SKILL.md metadata
    for skill_dir in sorted(SKILLS_DIR.iterdir()):
        skill_md = skill_dir / "SKILL.md"
        if skill_md.exists():
            skill_meta = load_skill_meta(skill_md)
            name = skill_meta.get("name", skill_dir.name)
            if name in metas:
                metas[name]["skill_md"] = skill_meta

    return registry, descriptions, metas


# Auto-discover on import
SKILL_REGISTRY, SKILL_DESCRIPTIONS, SKILL_META = discover_skills()
