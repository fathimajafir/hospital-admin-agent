"""Framework-free MCP bridge.

All protocol code lives in ``server.py`` (SDK) and ``fallback.py`` (stdlib). This
module only translates between the registry and the MCP wire shapes, which means
the two transports cannot expose different tools or bypass policy.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from hospital_agent.container import Container
from hospital_agent.errors import AgentError
from hospital_agent.models import Principal
from hospital_agent.tools.registry import ToolContext


@dataclass
class McpBridge:
    container: Container
    principal: Principal
    purpose: str

    # -- discovery ---------------------------------------------------------
    def list_tools(self) -> list[dict[str, Any]]:
        tools = []
        for tool in self.container.registry.catalog(self.principal):
            tools.append(
                {
                    "name": tool["name"],
                    "description": tool["description"],
                    "inputSchema": tool["inputSchema"],
                    "annotations": {
                        "title": tool["name"].replace("_", " ").title(),
                        "readOnlyHint": not tool["mutates"],
                        "destructiveHint": False,
                        "idempotentHint": not tool["mutates"],
                        "openWorldHint": False,
                    },
                }
            )
        return tools

    # -- invocation --------------------------------------------------------
    def call(self, name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
        ctx = ToolContext(
            store=self.container.store,
            principal=self.principal,
            purpose=self.purpose,
            settings=self.container.settings,
        )
        try:
            result = self.container.registry.call(name, arguments or {}, ctx)
        except AgentError as exc:
            return {
                "content": [{"type": "text", "text": json.dumps({"error": exc.to_dict()}, indent=2)}],
                "isError": True,
            }
        except Exception as exc:  # never let an MCP call kill the server loop
            return {
                "content": [{"type": "text", "text": json.dumps({"error": {"code": "internal", "message": str(exc)}})}],
                "isError": True,
            }

        payload = result.data if isinstance(result.data, dict) else {"value": result.data}
        return {
            "content": [{"type": "text", "text": json.dumps(payload, indent=2, default=str)}],
            "structuredContent": payload,
            "isError": False,
        }
