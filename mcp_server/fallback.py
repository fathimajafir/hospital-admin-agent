"""Dependency-free MCP stdio server.

Why this exists: the official ``mcp`` SDK is optional (``pip install -e .[mcp]``).
If it is missing, the server still has to work, because an MCP server that only
runs on one machine is not production-ready.

Implements the subset of MCP that clients actually use for tool servers:
``initialize``, ``notifications/initialized``, ``ping``, ``tools/list``,
``tools/call``. One JSON-RPC object per line, newline delimited.
"""

from __future__ import annotations

import json
import sys
from typing import Any, TextIO

from .bridge import McpBridge

PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_VERSIONS = {"2024-11-05", "2025-03-26", "2025-06-18"}


def handle(bridge: McpBridge, message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    request_id = message.get("id")

    if method is None:
        return None  # a response, not a request: nothing to do

    def ok(result: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def error(code: int, text: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": text}}

    if method == "initialize":
        requested = (message.get("params") or {}).get("protocolVersion")
        version = requested if requested in SUPPORTED_VERSIONS else PROTOCOL_VERSION
        return ok(
            {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "hospital-admin-agent", "version": "1.0.0"},
            }
        )

    if method in {"notifications/initialized", "initialized", "notifications/cancelled"}:
        return None

    if method == "ping":
        return ok({})

    if method == "tools/list":
        return ok({"tools": bridge.list_tools()})

    if method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        if not name:
            return error(-32602, "missing tool name")
        return ok(bridge.call(name, params.get("arguments") or {}))

    return error(-32601, f"method not found: {method}")


def serve(bridge: McpBridge, stdin: TextIO | None = None, stdout: TextIO | None = None) -> None:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            stdout.write(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}) + "\n")
            stdout.flush()
            continue

        response = handle(bridge, message)
        if response is not None:
            stdout.write(json.dumps(response, default=str) + "\n")
            stdout.flush()
