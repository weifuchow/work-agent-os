"""Message processing context.

The context intentionally contains platform and workflow facts only. Domain
interpretation belongs to skills.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class MessageContext:
    message: dict[str, Any]
    session: dict[str, Any] | None
    history: list[dict[str, Any]]
    attempt: int = 1

    @property
    def message_id(self) -> int:
        return int(self.message["id"])

    @property
    def session_id(self) -> int | None:
        if not self.session:
            return None
        value = self.session.get("id")
        return int(value) if value is not None else None


@dataclass(frozen=True)
class PreparedWorkspace:
    path: Path
    input_dir: Path
    state_dir: Path
    output_dir: Path
    artifacts_dir: Path
    artifact_roots: dict[str, str]
    media_manifest: dict[str, Any]
    skill_registry: dict[str, Any]
