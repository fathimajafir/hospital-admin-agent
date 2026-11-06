# Runbook

## Deploy checklist

- [ ] `HA_API_KEYS` set to real keys, one per integration — **never** the dev default
- [ ] `HA_LLM_PROVIDER` set (`deterministic` is a valid production choice for read-only use)
- [ ] `HA_DB_PATH` on a persistent volume, owned by uid 10001
- [ ] `HA_ENV=prod`, TLS terminated in front (the app does not do TLS)
- [ ] Budget alert on the model provider account
- [ ] `GET /readyz` returns `status: ready` and the expected tool count (14)
- [ ] `GET /v1/audit/verify` returns `intact: true`
- [ ] MCP processes run with the **lowest** role that does the job (`analyst` by default)
- [ ] If an EHR is connected: `HA_FHIR_BASE_URL`/`HA_FHIR_TOKEN` set, sync scheduled, and
      `HA_FHIR_WRITE_ENABLED` still `false` until a read-only pilot is clean

```bash
docker compose up --build -d
docker compose logs -f api | jq -r 'select(.level=="ERROR")'
curl -s localhost:8080/readyz
```

## Key rotation

Keys are read at startup, so rotation is a restart:

1. Add the new key to `HA_API_KEYS` alongside the old one.
2. Restart. Both keys work.
3. Move clients over.
4. Remove the old key and restart again.

The audit log records `actor` (principal `sub`), not the key — rotation does not break attribution.

## FHIR sync

Runs as a scheduled job, not from the API process. There is no queue and no
background worker: `ha-agent fhir sync` is a batch command that exits.

```bash
# 03:15 nightly, dry run first when changing the mapping or the scopes
15 3 * * *  cd /srv/hospital-admin-agent && HA_FHIR_BASE_URL=... \
            .venv/bin/ha-agent fhir sync --from "$HA_FHIR_BASE_URL" \
            >> /var/log/ha-fhir-sync.log 2>&1
```

Reading the report:

| Field | Meaning |
|---|---|
| `created` | new rows. A steady nightly number means patients are being duplicated — check `fhir_links` |
| `updated` | normal. Everything already known |
| `skipped` | not loaded. Each one has a `warnings[]` entry naming the resource and the reason |
| `warnings_dropped` | above 200 the list is truncated; the count still tells you the size of the problem |

- **401/403** — token missing, expired or missing scopes. The error text carries the
  server's `OperationOutcome` diagnostics. Rotate `HA_FHIR_TOKEN` and re-run; the
  job is idempotent, so a re-run after a partial failure is safe.
- **502 on booking** — the server rejected an approved appointment. Nothing was
  written locally; the approval is still executable, so fix the cause and retry
  the approval. Do not re-book by hand.
- **`created` growing every night** — the MRN identifier system probably changed.
  Check `HA_FHIR_MRN_SYSTEM` against what the server actually sends.
- **302 in an error** — the base URL redirects. Set `HA_FHIR_BASE_URL` to the final
  address; redirects are not followed on purpose (the token would go with them).

Turning on `HA_FHIR_WRITE_ENABLED` is a one-way door for workflow: after the first
successful write-back, staff will expect the EHR and the schedule to agree. Run a
read-only pilot first, and keep the flag off until the mapping report is clean.

## Approvals

```bash
# what is waiting?
curl -s -H "X-API-Key: $KEY" localhost:8080/v1/approvals | jq

# decide (must be a different principal than the requester)
curl -s -X POST localhost:8080/v1/approvals/apv_123/decide \
  -H "X-API-Key: $SECOND_KEY" -H 'content-type: application/json' \
  -d '{"decision":"approve","reason":"verified with charge nurse"}'

# continue the paused run
curl -s -X POST "localhost:8080/v1/runs/run_abc/resume?approval_id=apv_123" -H "X-API-Key: $KEY"
```

Expired approvals (past `HA_APPROVAL_TTL_S`) cannot be executed; request the action again.
A denied approval cannot be reversed — request a new one, which leaves a clean trail.

## Audit

```bash
curl -s -H "X-API-Key: $KEY" localhost:8080/v1/audit/verify
# {"intact": true, "detail": "ok", ...}

curl -s -H "X-API-Key: $KEY" 'localhost:8080/v1/audit?limit=20' | jq '.entries[] | {ts,actor,tool,outcome}'
```

If `verify` reports a break, **treat it as a security incident**: the detail names the first bad
row id. Stop writes, snapshot the database, and preserve the file before investigating. Do not
"repair" the chain — a broken chain is evidence.

## Common alerts and what they mean

| Symptom | Likely cause | Action |
|---|---|---|
| `readyz` 503 | store unreachable / disk full | check volume + permissions (uid 10001) |
| `upstream_error` on runs | LLM provider down or key invalid | switch to `deterministic`, or fix the key |
| `budget_exceeded` rising | vague tasks, or a looping tool | inspect `GET /v1/runs/{id}` steps; tighten the task |
| `policy_denied` spike | client picked the wrong role/purpose | check `/v1/me` and the client config |
| 429s | per-key rate limit | raise `HA_RATE_LIMIT_RPM` or split keys per integration |
| `approval_required` piling up | nobody is deciding them | alert on `len(pending)` > N |
| `verify` fails | tampering or disk corruption | incident procedure above |

## When the agent gives a bad answer

1. `GET /v1/runs/{id}` → look at `steps`: which tool, which arguments, what came back.
2. If the **wrong tool** was chosen → routing/prompt issue. Add an evaluation case first, then
   change the description.
3. If the **tool data** was wrong → data or seed issue, not the model.
4. If the **number** was wrong → check `tools/scoring.py`; it is deterministic, so it reproduces.

Never "fix" a case by loosening the assertion. The eval suite is the contract.

## Backup and restore

```bash
sqlite3 "$HA_DB_PATH" ".backup '/backup/hospital-$(date +%F).db'"
```

Restore by stopping the API, replacing the file, and starting again. The audit chain is inside the
same file, so a consistent backup is a consistent chain.

## MCP client troubleshooting

| Symptom | Cause |
|---|---|
| No tools listed | role has no permitted tools — check `HA_MCP_ROLE` |
| `spawn` error in the client | wrong `command` path; use the absolute `.venv/bin/python` |
| Tools listed, calls fail with `policy_denied` | arguments need `purpose_of_use`? tools use `HA_MCP_PURPOSE` — set it to a purpose granted to the role |
| Server exits immediately | check the `env` block; `HA_DB_PATH` directory must exist and be writable |
| Client sees 14 tools, analyst sees fewer | expected: the catalog is filtered by role |

Verify outside the client first:

```bash
python -m mcp_server.server --list | jq 'length'
```
