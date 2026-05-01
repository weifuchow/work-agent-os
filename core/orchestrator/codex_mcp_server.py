"""Expose work-agent MCP tools over stdio for Codex CLI."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import anyio
from claude_agent_sdk import create_sdk_mcp_server
from mcp.server.lowlevel.server import NotificationOptions
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.chdir(PROJECT_ROOT)

from core.orchestrator.tools import ORCHESTRATOR_TOOLS, PROJECT_TOOLS  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run work-agent MCP server for Codex CLI.")
    parser.add_argument(
        "--scope",
        choices=("orchestrator", "project"),
        default="orchestrator",
        help="Tool scope to expose.",
    )
    return parser.parse_args()


def _build_server(scope: str):
    tools = ORCHESTRATOR_TOOLS if scope == "orchestrator" else PROJECT_TOOLS
    config = create_sdk_mcp_server(
        name="work-agent-tools",
        version="1.0.0",
        tools=tools,
    )
    return config["instance"]


async def _serve(scope: str) -> None:
    server = _build_server(scope)
    init_options = InitializationOptions(
        server_name="work-agent-tools",
        server_version="1.0.0",
        capabilities=server.get_capabilities(NotificationOptions(), {}),
    )

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, init_options)


def main() -> None:
    args = _parse_args()
    anyio.run(_serve, args.scope)


if __name__ == "__main__":
    main()
