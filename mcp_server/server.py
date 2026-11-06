"""MCP server over stdio (and optionally streamable HTTP).

The MCP surface is intentionally thin: it exposes exactly the tools the calling
principal may use, and every call goes through the same registry, policy engine
and audit log as the HTTP API. There is no second implementation of a capability
to keep in sync - which is the main failure mode of "we also added an MCP server".

Run:
    python -m mcp_server.server                  # stdio (for Claude Desktop, Cursor, ...)
    python -m mcp_server.server --transport http # streamable HTTP at /mcp
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

import mcp.types as types
from mcp.server.lowlevel import Server

from hospital_agent.config import Settings
from hospital_agent.container import Container
from hospital_agent.models import Principal, Role

from .bridge import McpBridge

SERVER_NAME = "hospital-admin-agent"
SERVER_VERSION = "1.0.0"


def build_principal() -> Principal:
    role = os.getenv("HA_MCP_ROLE", "admin")
    purposes = os.getenv("HA_MCP_PURPOSE", "operations,treatment,payment")
    return Principal(
        sub=os.getenv("HA_MCP_SUBJECT", f"mcp-{role}"),
        role=Role(role),
        tenant=os.getenv("HA_MCP_TENANT", "default"),
        purposes=tuple(p.strip() for p in purposes.split(",") if p.strip()),
    )


def build_bridge(container: Container | None = None) -> McpBridge:
    container = container or Container.build(Settings())
    return McpBridge(
        container=container,
        principal=build_principal(),
        purpose=os.getenv("HA_MCP_PURPOSE", "operations").split(",")[0],
    )


def build_server(bridge: McpBridge) -> Server:
    server: Server = Server(SERVER_NAME, version=SERVER_VERSION)

    async def list_tools(_ctx: Any, _request: types.ListToolsRequest) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[types.Tool.model_validate(tool) for tool in bridge.list_tools()]
        )

    async def call_tool(_ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        # The framework validates incoming `params` against the type registered
        # below and hands the parsed params object to the handler.
        result = bridge.call(params.name, params.arguments or {})
        return types.CallToolResult.model_validate(result)

    server.add_request_handler("tools/list", types.ListToolsRequest, list_tools)
    server.add_request_handler("tools/call", types.CallToolRequestParams, call_tool)
    return server


async def run_stdio(bridge: McpBridge | None = None) -> None:
    from mcp.server.stdio import stdio_server

    server = build_server(bridge or build_bridge())
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def run_http(bridge: McpBridge | None = None, host: str = "127.0.0.1", port: int = 8765) -> None:
    import uvicorn

    server = build_server(bridge or build_bridge())
    app = server.streamable_http_app(host=host)
    uvicorn.run(app, host=host, port=port, log_level="info")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hospital-mcp", description="MCP server for the hospital administration agent")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--list", action="store_true", help="print the tool catalog and exit")
    args = parser.parse_args(argv)

    if args.list:
        bridge = build_bridge()
        import json

        print(json.dumps(bridge.list_tools(), indent=2))
        return 0

    if args.transport == "http":
        run_http(host=args.host, port=args.port)
        return 0

    import asyncio

    try:
        asyncio.run(run_stdio())
    except KeyboardInterrupt:  # pragma: no cover
        return 130
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
