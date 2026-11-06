from __future__ import annotations

import io
import json

import pytest

from hospital_agent.models import Principal, Role
from mcp_server import fallback
from mcp_server.bridge import McpBridge


@pytest.fixture()
def bridge(container) -> McpBridge:
    return McpBridge(
        container=container,
        principal=Principal(sub="mcp", role=Role.admin, purposes=("operations", "treatment")),
        purpose="operations",
    )


def test_bridge_exposes_every_authorised_tool(bridge: McpBridge) -> None:
    tools = bridge.list_tools()
    names = {t["name"] for t in tools}
    assert len(tools) == 14
    assert {"capacity_snapshot", "ops_digest", "patient_get"} <= names
    for tool in tools:
        assert tool["inputSchema"]["type"] == "object"
        assert tool["description"]
        assert tool["annotations"]["readOnlyHint"] in {True, False}


def test_bridge_call_returns_text_and_structured_content(bridge: McpBridge) -> None:
    result = bridge.call("capacity_snapshot", {})
    assert result["isError"] is False
    assert result["content"][0]["type"] == "text"
    payload = json.loads(result["content"][0]["text"])
    assert payload["totals"]["beds"] == 54
    assert result["structuredContent"]["totals"]["beds"] == 54


def test_bridge_returns_phi_to_an_authorised_clinician(container) -> None:
    clinician = McpBridge(
        container=container,
        principal=Principal(sub="mcp-clin", role=Role.clinician, purposes=("treatment",)),
        purpose="treatment",
    )
    payload = json.loads(clinician.call("patient_search", {"query": "P0005"})["content"][0]["text"])
    assert payload["results"][0]["name"] == "Test Patient 05"


def test_bridge_masks_phi_when_the_purpose_policy_is_disabled(container) -> None:
    from hospital_agent.policy.engine import PolicyEngine

    container.registry.policy = PolicyEngine.from_file(None, require_phi_purpose=False)
    restricted = McpBridge(
        container=container,
        principal=Principal(sub="mcp-clin", role=Role.clinician, purposes=()),
        purpose="",
    )
    payload = json.loads(restricted.call("patient_search", {"query": "P0005"})["content"][0]["text"])
    assert payload["results"][0]["name"] == "T*** P***"


def test_bridge_returns_iserror_for_policy_denial(container) -> None:
    analyst = McpBridge(
        container=container,
        principal=Principal(sub="mcp-a", role=Role.analyst, purposes=("operations",)),
        purpose="operations",
    )
    result = analyst.call("patient_get", {"patient_id": "P0001"})
    assert result["isError"] is True
    assert "policy_denied" in result["content"][0]["text"]


def test_fallback_handshake(bridge: McpBridge) -> None:
    response = fallback.handle(
        bridge, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}
    )
    assert response["result"]["protocolVersion"] == "2025-06-18"
    assert "tools" in response["result"]["capabilities"]
    assert response["result"]["serverInfo"]["name"] == "hospital-admin-agent"


def test_fallback_initialized_notification_has_no_response(bridge: McpBridge) -> None:
    assert fallback.handle(bridge, {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_fallback_tools_list_and_call(bridge: McpBridge) -> None:
    listed = fallback.handle(bridge, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert len(listed["result"]["tools"]) == 14

    called = fallback.handle(
        bridge,
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "ops_digest", "arguments": {}}},
    )
    assert called["result"]["isError"] is False
    assert "headline" in called["result"]["content"][0]["text"]


def test_fallback_unknown_method(bridge: McpBridge) -> None:
    response = fallback.handle(bridge, {"jsonrpc": "2.0", "id": 4, "method": "resources/list"})
    assert response["error"]["code"] == -32601


def test_fallback_stdio_loop(bridge: McpBridge) -> None:
    lines = "\n".join(
        [
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
            "not json",
        ]
    )
    stdin = io.StringIO(lines + "\n")
    stdout = io.StringIO()
    fallback.serve(bridge, stdin=stdin, stdout=stdout)

    responses = [json.loads(line) for line in stdout.getvalue().strip().splitlines()]
    assert len(responses) == 3
    assert responses[0]["result"]["protocolVersion"]
    assert len(responses[1]["result"]["tools"]) == 14
    assert responses[2]["error"]["code"] == -32700
