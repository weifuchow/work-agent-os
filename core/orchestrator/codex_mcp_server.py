"""Expose work-agent MCP tools over stdio for Codex CLI."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import anyio
from mcp.server.lowlevel.server import NotificationOptions
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
from mcp.server import Server
from mcp.types import CallToolResult, TextContent, Tool

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
    server = Server("work-agent-tools", version="1.0.0")
    tool_map = {tool_def.name: tool_def for tool_def in tools}
    tool_list = [
        Tool(
            name=tool_def.name,
            description=tool_def.description,
            inputSchema=tool_def.input_schema if isinstance(tool_def.input_schema, dict) else {"type": "object"},
            annotations=tool_def.annotations,
        )
        for tool_def in tools
    ]

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return tool_list

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> CallToolResult:
        if name not in tool_map:
            return _tool_result({"error": f"Tool '{name}' not found"}, is_error=True)

        try:
            payload = await tool_map[name].handler(arguments or {})
        except Exception as exc:
            return _tool_result({"error": str(exc)}, is_error=True)

        is_error = isinstance(payload, dict) and bool(payload.get("error"))
        return _tool_result(payload, is_error=is_error)

    return server


def _tool_result(payload: object, *, is_error: bool = False) -> CallToolResult:
    text = json_dumps(payload)
    structured = payload if isinstance(payload, dict) else None
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structuredContent=structured,
        isError=is_error,
    )


def json_dumps(payload: object) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


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
