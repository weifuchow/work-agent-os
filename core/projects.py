"""Project registry — loads projects from data/projects.yaml, caches, merges skills."""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re
import subprocess
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


@dataclass
class ProjectGitMeta:
    branch: str = ""
    commit_sha: str = ""
    commit_time: datetime | None = None
    describe: str = ""
    is_dirty: bool = False
    version_hint: str = ""


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


def get_project_git_meta(name: str | None = None, path: Path | None = None) -> ProjectGitMeta:
    project_path = path.resolve() if path else None
    if name and not project_path:
        project = get_project(name)
        project_path = project.path if project else None
    if not project_path or not project_path.exists():
        return ProjectGitMeta()

    git_dir = project_path / ".git"
    if not git_dir.exists():
        return ProjectGitMeta()

    branch = _git_capture(project_path, "rev-parse", "--abbrev-ref", "HEAD")
    commit_sha = _git_capture(project_path, "rev-parse", "--short=12", "HEAD")
    commit_time_raw = _git_capture(project_path, "show", "-s", "--format=%cI", "HEAD")
    describe = _git_capture(project_path, "describe", "--tags", "--always", "--dirty")
    dirty = bool(_git_capture(project_path, "status", "--porcelain", "-uno"))
    version_hint = infer_version_from_git(branch=branch, describe=describe, commit_sha=commit_sha)

    commit_time = None
    if commit_time_raw:
        try:
            commit_time = datetime.fromisoformat(commit_time_raw)
        except ValueError:
            commit_time = None

    return ProjectGitMeta(
        branch=branch,
        commit_sha=commit_sha,
        commit_time=commit_time,
        describe=describe,
        is_dirty=dirty,
        version_hint=version_hint,
    )


def infer_version_from_git(
    *,
    branch: str = "",
    describe: str = "",
    commit_sha: str = "",
) -> str:
    for source in [branch, describe]:
        normalized = _extract_semver_like(source)
        if normalized:
            return normalized

    clean_branch = (branch or "").strip()
    if clean_branch and clean_branch not in {"HEAD", "main", "master", "develop", "dev"}:
        if commit_sha:
            return f"{clean_branch}@{commit_sha[:8]}"
        return clean_branch

    clean_describe = (describe or "").strip()
    if clean_describe:
        return clean_describe

    return (commit_sha or "")[:8]


def _git_capture(project_path: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(project_path), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=5,
            check=False,
        )
    except Exception:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _extract_semver_like(text: str) -> str:
    if not text:
        return ""

    patterns = [
        r"\b[vV](\d+(?:\.\d+){1,4})\b",
        r"(?:release|hotfix|版本|version|sprint)[/_\- ]*([A-Za-z]*\d+(?:\.\d+){1,4})\b",
        r"\b(?:RIOT|Riot|FMS|Allspark)[/_\- ]*(\d+(?:\.\d+){0,4})\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1).strip()
    return ""


def merge_skills(
    global_skills: dict[str, AgentDefinition],
    project_path: Path,
    include_global: bool = True,
) -> dict[str, AgentDefinition]:
    """Merge project-local skills over global skills (project wins on name collision).

    Scans {project_path}/.claude/agents/*.md and {project_path}/.claude/skills/*/SKILL.md.
    Returns a new dict; does not mutate global_skills.
    """
    from skills import discover_skills

    agents_dir = project_path / ".claude" / "agents"
    skills_dir = project_path / ".claude" / "skills"

    project_skills, _ = discover_skills(agents_dir=agents_dir, skills_dir=skills_dir)

    if not project_skills:
        return dict(global_skills) if include_global else {}

    merged = dict(global_skills) if include_global else {}
    for name, defn in project_skills.items():
        if name in merged:
            logger.info("Project skill '{}' overrides global skill", name)
        merged[name] = defn

    logger.info("Merged skills: {} global + {} project = {} total (include_global={})",
                len(global_skills), len(project_skills), len(merged), include_global)
    return merged
