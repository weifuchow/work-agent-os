"""Public message processing facade."""

from __future__ import annotations

from pathlib import Path

from core.config import settings
from core.deps import CoreDependencies, get_dependencies, set_dependency_override
from core.app.message_processor import MessageProcessor


DB_PATH = str(Path(settings.db_dir) / "app.sqlite")


async def process_message(message_id: int) -> None:
    """Process a single message through the core workflow."""

    deps = get_dependencies(DB_PATH)
    await MessageProcessor(deps.processor_deps()).process(message_id)


async def reprocess_message(message_id: int) -> None:
    """Reset message pipeline state and process it again."""

    deps = get_dependencies(DB_PATH)
    await deps.repository.reset_message_for_reprocess(message_id)
    await MessageProcessor(deps.processor_deps()).process(message_id)


def configure_dependencies_for_tests(dependencies: CoreDependencies | None) -> None:
    """Override core dependencies for public-contract tests."""

    set_dependency_override(dependencies)
