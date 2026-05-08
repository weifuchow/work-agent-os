"""Compatibility entrypoint for the main orchestrator E2E scenario runner.

The implementation lives inside the main-orchestrator-project-coordination
skill so orchestration-specific diagnostics stay with the orchestration
playbook. Keep this root script for existing commands and docs.
"""

from __future__ import annotations

import runpy
from pathlib import Path


SKILL_SCRIPT = (
    Path(__file__).resolve().parent.parent
    / ".claude"
    / "skills"
    / "main-orchestrator-project-coordination"
    / "scripts"
    / "e2e_scenario.py"
)


if __name__ == "__main__":
    runpy.run_path(str(SKILL_SCRIPT), run_name="__main__")
