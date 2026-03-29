"""Project registry — loads projects from data/projects.yaml, caches, merges skills."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from claude_agent_sdk import AgentDefinition
from loguru import logger

from core.config import settings


@dataclass
class ProjectConfig:
    name: str
    path: Path
    description: str


_cache: list[ProjectConfig] | None = None


def load_projects() -> list[ProjectConfig]:
    """Read data/projects.yaml and return validated project configs."""
    projects_file = settings.projects_file
    if not projects_file.exists():
        logger.warning("Projects file not found: {}", projects_file)
        return []

    try:
        raw = yaml.safe_load(projects_file.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error("Failed to parse projects.yaml: {}", e)
        return []

    if not raw or not isinstance(raw.get("projects"), list):
        return []

    result: list[ProjectConfig] = []
    for entry in raw["projects"]:
        name = entry.get("name", "").strip()
        path_str = entry.get("path", "").strip()
        desc = entry.get("description", "").strip()

        if not name or not path_str:
            logger.warning("Skipping project entry with missing name or path: {}", entry)
            continue

        path = Path(path_str).resolve()
        if not path.exists():
            logger.warning("Project '{}' path does not exist: {}", name, path)
            continue

        result.append(ProjectConfig(name=name, path=path, description=desc))

    logger.info("Loaded {} projects from {}", len(result), projects_file)
    return result


def get_projects() -> list[ProjectConfig]:
    """Get cached project list (lazy load on first call)."""
    global _cache
    if _cache is None:
        _cache = load_projects()
    return _cache


def get_project(name: str) -> ProjectConfig | None:
    """Look up a project by name."""
    for p in get_projects():
        if p.name == name:
            return p
    return None


def refresh_projects() -> list[ProjectConfig]:
    """Clear cache and reload from disk."""
    global _cache
    _cache = None
    return get_projects()


def merge_skills(
    global_skills: dict[str, AgentDefinition],
    project_path: Path,
) -> dict[str, AgentDefinition]:
    """Merge project-local skills over global skills (project wins on name collision).

    Scans {project_path}/.claude/agents/*.md for project skills.
    Returns a new dict; does not mutate global_skills.
    """
    from skills import discover_skills

    agents_dir = project_path / ".claude" / "agents"

    project_skills, _ = discover_skills(agents_dir=agents_dir)

    if not project_skills:
        return dict(global_skills)

    merged = dict(global_skills)
    for name, defn in project_skills.items():
        if name in merged:
            logger.info("Project skill '{}' overrides global skill", name)
        merged[name] = defn

    logger.info("Merged skills: {} global + {} project = {} total",
                len(global_skills), len(project_skills), len(merged))
    return merged
