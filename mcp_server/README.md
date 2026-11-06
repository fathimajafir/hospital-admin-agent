# MCP server

Exposes the hospital administration agent's tools to any MCP client (Claude Desktop, Cursor,
Claude Code, custom agents).

It is a **thin adapter**. `bridge.py` translates registry tools into MCP shapes; `server.py` speaks
the protocol. No tool logic lives here, so the MCP surface cannot drift from the HTTP API.

## Run

```bash
# stdio (normal for desktop clients)
python -m mcp_server.server

# show what this role can do
python -m mcp_server.server --list

# streamable HTTP
python -m mcp_server.server --transport http --host 127.0.0.1 --port 8765
```

If the `mcp` SDK is not installed (`pip install -e .[mcp]`), `fallback.py` provides a stdlib
JSON-RPC implementation of `initialize`, `tools/list` and `tools/call`. Same bridge, same policy.

## Client configuration

### Claude Desktop

`~/Library/Application Support/Claude/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "hospital-admin": {
      "command": "/Users/fathima.kunnummal/Documents/hospital-admin-agent/.venv/bin/python",
      "args": ["-m", "mcp_server.server"],
      "cwd": "/Users/fathima.kunnummal/Documents/hospital-admin-agent",
      "env": {
        "HA_DB_PATH": "/Users/fathima.kunnummal/Documents/hospital-admin-agent/var/hospital.db",
        "HA_MCP_ROLE": "analyst",
        "HA_MCP_PURPOSE": "operations",
        "HA_MCP_SUBJECT": "claude-desktop"
      }
    }
  }
}
```

### Cursor

`.cursor/mcp.json` in the project, same block under `"mcpServers"`.

### HTTP

```json
{
  "mcpServers": {
    "hospital-admin": { "url": "http://127.0.0.1:8765/mcp" }
  }
}
```

## Roles

`HA_MCP_ROLE` decides which tools are even listed:

| Role | Sees | Does not see |
|---|---|---|
| `service` | read-only flow tools | writes, PHI, financial |
| `analyst` | flow, staffing, scheduling (read), quality | `patient_get`, `patient_search`, `claim_scrub`, `billing_summary` |
| `scheduler` | + `schedule_appointment` (approval-gated) | PHI, financial |
| `billing` | + claims and prior auth | clinical writes |
| `clinician` | + PHI and `bed_assign` | financial |
| `admin` | everything | — |

`HA_MCP_PURPOSE` is the purpose of use sent with every call. It must be a purpose granted to the
role, or PHI/financial tools will deny.

## What a client sees

`tools/list` returns, per tool: `name`, `description`, `inputSchema` (real JSON Schema from the
Pydantic model) and annotations (`readOnlyHint` is true for every non-mutating tool).

`tools/call` returns pretty-printed JSON in `content[0].text` **and** `structuredContent` for
clients that support it. Policy denials come back as `isError: true` with a machine-readable
`{"error": {"code": "policy_denied", ...}}` body — MCP has no status codes, so the code travels in
the payload.

## Security notes

- The MCP process inherits the same guardrails: role, purpose, approvals, redaction, audit.
- A write tool called over MCP **will not execute**; it returns an approval id. Approve it through
  the HTTP API (`POST /v1/approvals/{id}/decide`) — a chat client is not a place to self-approve.
- Run the MCP server with its own role and subject so the audit log distinguishes it from the API.
- Do not expose the HTTP transport beyond localhost without putting an authenticating proxy in
  front of it.
