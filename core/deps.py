"""Dependency assembly for core workflow."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from core.agents.runner import AgentRunner, DefaultAgentPort
from core.app.message_processor import MessageProcessorDeps
from core.app.result_handler import ResultHandler
from core.artifacts.workspace import WorkspacePreparer
from core.connectors.feishu import FeishuChannelPort, FeishuFilePort
from core.ports import ClockPort
from core.repositories import Repository
from core.sessions.service import SessionService


class SystemClock:
    def now_iso(self) -> str:
        return datetime.now().isoformat()


@dataclass(frozen=True)
class CoreDependencies:
    repository: Repository
    sessions: SessionService
    workspaces: WorkspacePreparer
    agents: AgentRunner
    result_handler: ResultHandler
    clock: ClockPort

    def processor_deps(self) -> MessageProcessorDeps:
        return MessageProcessorDeps(
            repository=self.repository,
            sessions=self.sessions,
            workspaces=self.workspaces,
            agents=self.agents,
            result_handler=self.result_handler,
            clock=self.clock,
        )


_override: CoreDependencies | None = None


def build_dependencies(db_path: str | Path | None = None) -> CoreDependencies:
    repository = Repository(db_path)
    sessions = SessionService(repository)
    clock = SystemClock()
    channel = FeishuChannelPort()
    workspaces = WorkspacePreparer(FeishuFilePort())
    agents = AgentRunner(DefaultAgentPort())
    result_handler = ResultHandler(
        repository=repository,
        channel_port=channel,
        clock=clock,
        reply_repairer=agents,
    )
    return CoreDependencies(
        repository=repository,
        sessions=sessions,
        workspaces=workspaces,
        agents=agents,
        result_handler=result_handler,
        clock=clock,
    )


def get_dependencies(db_path: str | Path | None = None) -> CoreDependencies:
    return _override or build_dependencies(db_path)


def set_dependency_override(dependencies: CoreDependencies | None) -> None:
    global _override
    _override = dependencies
