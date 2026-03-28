"""Skill registry — discovers agent definitions from .claude/agents/*.md.

Agent definitions (.claude/agents/*.md) are the single source of truth.
SKILL.md files under skills/ are documentation only.
"""

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
    current_key = None

    for line in parts[1].strip().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        # List item
        if stripped.startswith("- ") and current_key and isinstance(meta.get(current_key), list):
            meta[current_key].append(stripped[2:].strip())
            continue

        # Key: value
        if ":" in stripped:
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

    body = parts[2].strip()
    return meta, body


def discover_skills() -> tuple[dict[str, AgentDefinition], dict[str, str]]:
    """Discover all agent definitions from .claude/agents/*.md.

    Returns:
        (SKILL_REGISTRY, SKILL_DESCRIPTIONS)
    """
    registry: dict[str, AgentDefinition] = {}
    descriptions: dict[str, str] = {}

    if not AGENTS_DIR.exists():
        return registry, descriptions

    for md_file in sorted(AGENTS_DIR.glob("*.md")):
        text = md_file.read_text(encoding="utf-8")
        meta, body = _parse_frontmatter(text)

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

        registry[name] = definition
        descriptions[name] = description

    return registry, descriptions


# Auto-discover on import
SKILL_REGISTRY, SKILL_DESCRIPTIONS = discover_skills()
