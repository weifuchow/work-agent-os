from pathlib import Path

from claude_agent_sdk import AgentDefinition

from core.projects import merge_skills
import core.skill_registry as skills_mod


def _write_skill(md_file: Path, name: str, description: str, body: str = "# Prompt\n\nUse this skill.") -> None:
    md_file.parent.mkdir(parents=True, exist_ok=True)
    md_file.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n",
        encoding="utf-8",
    )


def test_discover_skills_loads_default_agents_and_skills_dirs(monkeypatch, tmp_path):
    agents_dir = tmp_path / ".claude" / "agents"
    skills_dir = tmp_path / ".claude" / "skills"

    _write_skill(agents_dir / "flat-agent.md", "flat-agent", "flat agent description")
    _write_skill(skills_dir / "folder-skill" / "SKILL.md", "folder-skill", "folder skill description")

    monkeypatch.setattr(skills_mod, "DEFAULT_AGENTS_DIR", agents_dir)
    monkeypatch.setattr(skills_mod, "DEFAULT_SKILLS_DIR", skills_dir)

    registry, descriptions = skills_mod.discover_skills()

    assert set(registry) == {"flat-agent", "folder-skill"}
    assert descriptions["flat-agent"] == "flat agent description"
    assert descriptions["folder-skill"] == "folder skill description"


def test_merge_skills_keeps_global_entries_and_project_overrides(tmp_path):
    project_path = tmp_path / "project"
    project_skills_dir = project_path / ".claude" / "skills"

    _write_skill(project_skills_dir / "shared-skill" / "SKILL.md", "shared-skill", "project override description")
    _write_skill(project_skills_dir / "project-only" / "SKILL.md", "project-only", "project only description")

    global_skills = {
        "shared-skill": AgentDefinition(
            description="global shared description",
            prompt="global shared prompt",
        ),
        "global-only": AgentDefinition(
            description="global only description",
            prompt="global only prompt",
        ),
    }

    merged = merge_skills(global_skills, project_path, include_global=True)

    assert set(merged) == {"shared-skill", "global-only", "project-only"}
    assert merged["shared-skill"].description == "project override description"
    assert merged["global-only"].description == "global only description"
    assert merged["project-only"].description == "project only description"
