# Hospital Administration Agent

A production-shaped AI agent for hospital **operations** — patient flow, discharge planning,
staffing, access and revenue cycle — plus an **MCP server** that exposes the same capabilities to
any MCP client.

The design goal is not "a chatbot that knows about hospitals". It is a system where an agent can
take operational actions safely: every capability is a typed tool, every call is authorised,
every write needs a second human, and every action lands in a tamper-evident audit log.

```
                    ┌──────────────────────────────────────────────┐
   HTTP / SSE ─────▶│  FastAPI  ·  auth · rate limit · metrics      │
   MCP stdio/HTTP ─▶│  MCP server (thin adapter)                    │
                    └───────────────────┬──────────────────────────┘
                                        │  one choke point
                    ┌───────────────────▼──────────────────────────┐
                    │  ToolRegistry                                │
                    │  policy → approval → validate → execute      │
                    │  → redact → audit (hash chain) → idempotency │
                    └───────┬───────────────────────┬──────────────┘
                            │                       │
                    ┌───────▼────────┐     ┌────────▼─────────┐
                    │  14 tools      │     │  Agent loop      │
                    │  flow/revenue/ │     │  planner +       │
                    │  access/...    │     │  budgets         │
                    └───────┬────────┘     └────────┬─────────┘
                            │                       │
                    ┌───────▼───────────────────────▼──────────┐
                    │  Store (SQLite)  ·  Policy (JSON rules)   │
                    └───────────────────────────────────────────┘
```

---

## What it does (14 tools)

### Capacity & flow
| Tool | What it answers |
|---|---|
| `capacity_snapshot` | Occupancy, boarders, ED wait and a 0–1 pressure score per ward, with the next action |
| `discharge_board` | Which discharges are blocked, by whom, and what unblocks them |
| `bed_assign` | Place a patient in a bed **(approval required)** |
| `staffing_gaps` | Census × acuity vs the roster, per ward and shift |

### Access & scheduling
| Tool | What it answers |
|---|---|
| `appointment_risk` | No-show probability **with its drivers** and a mitigation |
| `find_appointment_slots` | Slots ranked by expected attendance × time-to-care |
| `schedule_appointment` | Book a slot **(approval required)** |

### Revenue cycle
| Tool | What it answers |
|---|---|
| `claim_scrub` | Pre-submission denial risk with the exact fix per finding |
| `billing_summary` | Risk-adjusted dollars by payer, priority claims first |
| `build_prior_auth_packet` | Required vs missing documents, draft justification, approval odds |

### Patient & analytics
| Tool | What it answers |
|---|---|
| `patient_get` / `patient_search` | Patient record, **PHI-masked** unless authorised (purpose required) |
| `ops_digest` | One headline + ranked actions with owners and impact |
| `quality_indicators` | Occupancy, boarders, projected no-show rate, denial share, readiness vs benchmarks |

### What is genuinely new here
Most "hospital agent" demos are CRUD wrappers. These are the differentiating pieces:

1. **Every score is explainable.** No-show risk returns per-factor contributions; denial risk
   returns a `fix` per finding; discharge readiness names the blocking owner. An operator can
   argue with the number instead of trusting it.
2. **Slot ranking is economic, not chronological.** `expected_attendance × time-to-care` changes
   which slot you offer, not just when the next gap is.
3. **Prior-auth packet assembly.** It compares the procedure's document requirements against what
   is actually on file and drafts the justification — the step that usually eats an hour of a
   coordinator's day.
4. **Approvals are a first-class state.** A write action pauses the run with an approval id, and
   the run is *resumable* — not a dead end. The requester cannot approve their own action.
5. **Policy is data, not code.** Role → permission grants and per-tool requirements live in
   `policy/rules.default.json`, reviewed as a diff.
6. **Tamper-evident audit.** Hash-chained, arguments stored as fingerprints (never raw PHI), with
   a `verify` endpoint that detects edits.
7. **Runs without an LLM.** A deterministic planner makes the whole system testable in CI at zero
   cost, and doubles as the fallback when the provider is down.

---

## Quickstart

```bash
cd /Users/fathima.kunnummal/Documents/hospital-admin-agent

make venv install      # Python 3.11 + deps
source .venv/bin/activate

make test              # 65 tests
make eval              # 13/13 evaluation cases
make demo              # three representative agent tasks

make run               # http://127.0.0.1:8080/docs
```

First requests:

```bash
curl -s localhost:8080/healthz
curl -s -H 'X-API-Key: dev-admin-key' localhost:8080/v1/tools | jq '.tools | length'
curl -s -X POST localhost:8080/v1/agent/run \
  -H 'X-API-Key: dev-admin-key' -H 'content-type: application/json' \
  -d '{"task":"give me the morning digest","purpose_of_use":"operations"}' | jq -r .answer
```

The default `HA_LLM_PROVIDER=deterministic` needs no key. To use a real model:

```bash
export HA_LLM_PROVIDER=openai
export HA_LLM_BASE_URL=https://api.openai.com/v1
export HA_LLM_API_KEY=sk-...
export HA_LLM_MODEL=gpt-4o-mini
```

The planner only needs an OpenAI-compatible `/chat/completions` that honours
`response_format={"type":"json_object"}` — so vLLM, Ollama, LiteLLM or a Bedrock gateway all work.

---

## HTTP API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/healthz`, `/readyz` | liveness / readiness |
| `GET` | `/metrics` | Prometheus text |
| `GET` | `/v1/me` | who you are, what you can call |
| `GET` | `/v1/tools` | tool catalog **filtered by your role** |
| `POST` | `/v1/tools/{tool}` | direct tool call (same path as the agent) |
| `POST` | `/v1/agent/run` | run a task |
| `POST` | `/v1/agent/stream` | run a task, SSE one event per step |
| `GET` | `/v1/runs/{id}` | run record |
| `POST` | `/v1/runs/{id}/resume?approval_id=…` | continue after approval |
| `GET` | `/v1/approvals` | pending + approved-awaiting-execution |
| `POST` | `/v1/approvals/{id}/decide` | approve or deny (different person) |
| `GET` | `/v1/audit`, `/v1/audit/verify` | audit log and chain verification |

Errors are typed and machine-readable:

```json
{"error": {"code": "policy_denied", "message": "role 'analyst' lacks permission(s): phi",
           "details": {"tool": "patient_get"}}}
```

A write tool returns **202** with an approval id, not an error:

```json
{"error": {"code": "approval_required", "message": "'schedule_appointment' requires human approval",
           "approval_id": "apv_9f2c1a77b3de"}}
```

### Approval round trip

```bash
# 1. request (pauses)
curl -X POST localhost:8080/v1/tools/schedule_appointment -H 'X-API-Key: admin-key' \
  -H 'content-type: application/json' -d '{"tool":"schedule_appointment","args":{
    "patient_id":"P0007","dept":"Cardiology","provider":"Dr. Rao","start_ts":"2027-02-01T10:00"},
    "purpose_of_use":"operations"}'
# -> 202 {"error":{"approval_id":"apv_..."}}

# 2. a different person decides
curl -X POST localhost:8080/v1/approvals/apv_.../decide -H 'X-API-Key: admin2-key' \
  -H 'content-type: application/json' -d '{"decision":"approve","reason":"confirmed with patient"}'

# 3. the run continues from where it stopped
curl -X POST 'localhost:8080/v1/runs/run_.../resume?approval_id=apv_...' -H 'X-API-Key: admin-key'
```

---

## MCP server

The MCP server is a **thin adapter**: it lists only the tools your role may use and routes every
call through the same registry, policy and audit as HTTP. There is no second implementation.

```bash
python -m mcp_server.server --list          # print the catalog
python -m mcp_server.server                 # stdio
python -m mcp_server.server --transport http --port 8765   # http://127.0.0.1:8765/mcp
```

Claude Desktop / Cursor (`claude_desktop_config.json`):

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
        "HA_MCP_PURPOSE": "operations"
      }
    }
  }
}
```

Set `HA_MCP_ROLE` to the least privilege you need. `analyst` sees flow, staffing, scheduling and
quality — and no PHI at all.

---

## Security model

Four layers, each independent of the others:

1. **Authentication** — `X-API-Key` (or `Authorization: Bearer`) maps to a principal
   `{sub, role, tenant, purposes}`. Keys live in `HA_API_KEYS`, never in code.
2. **Authorisation** — deny-by-default. A tool declares the permissions it needs; the role must
   hold all of them.
   | Role | Permissions |
   |---|---|
   | `service` | read |
   | `analyst` | read, analytics |
   | `scheduler` | read, write, schedule |
   | `billing` | read, write, financial |
   | `clinician` | read, write, clinical, phi |
   | `admin` | everything |
3. **Purpose of use** — any tool touching PHI additionally requires `purpose_of_use` ∈
   `{treatment, operations, payment, research}` **and** that purpose must be granted to the
   principal. A typo is rejected, not silently ignored.
4. **Approval** — `schedule_appointment`, `bed_assign` (and anything marked in the rules) create a
   pending approval with the exact arguments. A *different* person must approve. Approvals expire
   (`HA_APPROVAL_TTL_S`) and are single-use: after execution the status becomes `executed`.

Plus:
- **Minimum necessary.** A caller without PHI permission never receives it — not masked, absent.
- **Masking is still applied** when PHI tools are allowed without a purpose (config-dependent), and
  to logs and free text (`redact_text`).
- **Audit.** Every call, denial, approval and run transition is one hash-chained row. Arguments are
  stored as a SHA-256 fingerprint; sensitive detail fields are redacted or omitted.

---

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `HA_ENV` | `dev` | free-form environment label |
| `HA_DB_PATH` | `./var/hospital.db` | `:memory:` for tests |
| `HA_API_KEYS` | dev admin key | JSON map key → principal. **Set this in prod.** |
| `HA_LLM_PROVIDER` | `deterministic` | `deterministic` or `openai` |
| `HA_LLM_BASE_URL` / `HA_LLM_API_KEY` / `HA_LLM_MODEL` | — | any OpenAI-compatible endpoint |
| `HA_MAX_STEPS` | `8` | agent loop cap (hard max 20) |
| `HA_TOKEN_BUDGET` | `12000` | tokens per run |
| `HA_RUN_TIMEOUT_S` | `45` | wall-clock per run |
| `HA_REQUIRE_PHI_PURPOSE` | `true` | global switch for the purpose gate |
| `HA_APPROVAL_TTL_S` | `1800` | approval lifetime |
| `HA_RATE_LIMIT_RPM` | `240` | per API key, per process |
| `HA_MAX_BODY_BYTES` | `1048576` | request size cap |
| `HA_MCP_ROLE` / `HA_MCP_PURPOSE` | `admin` / `operations` | MCP principal |
| `HA_FHIR_BASE_URL` | empty | FHIR R4 base URL. Empty = no EHR, synthetic data only |
| `HA_FHIR_TOKEN` | empty | bearer credential; environment only, never logged |
| `HA_FHIR_MRN_SYSTEM` | empty | identifier system that defines the internal patient id |
| `HA_FHIR_PAGE_SIZE` / `HA_FHIR_MAX_PAGES` | `100` / `20` | bounds on every search |
| `HA_FHIR_WRITE_ENABLED` | `false` | post-approval Appointment write-back |

See `.env.example`.

---

## FHIR integration

Read the full mapping and its gaps in [`docs/FHIR.md`](docs/FHIR.md).

```bash
ha-agent fhir check --base-url https://hapi.fhir.org/baseR4
ha-agent fhir sync --from https://fhir.example.org/r4 --dry-run
ha-agent fhir sync --from https://fhir.example.org/r4
ha-agent fhir links --limit 20
```

Four resources are ingested — `Patient`, `Coverage`, `Appointment`, `Claim` — and
exactly one is written back: an approved `schedule_appointment` becomes a FHIR
`Appointment`. Two properties matter more than coverage:

- **Idempotent.** External identity lives in `fhir_links`, so a nightly full pull
  updates rows instead of duplicating them, and re-running reports zero creates.
- **Non-destructive.** FHIR only owns the fields it actually provides. Locally
  tuned `risk_score`, document checklists, prior no-show counts and travel
  distance survive a re-sync.

A resource that cannot be loaded faithfully is **skipped and reported**, not
defaulted: a Patient with no birth date, a Claim whose patient is unknown, an
Appointment in `entered-in-error`. The report carries ids and reasons, never
names or clinical text.

Write-back is ordered FHIR-first: if the server rejects the appointment, no local
row is created and the approval can be retried. The alternative leaves the
schedule and the EHR disagreeing.

---

## Operations

```bash
cp .env.example .env             # then set HA_API_KEYS to real keys
docker compose up --build        # API :8080, MCP :8765
make docker                      # image only
curl localhost:8080/metrics      # ha_http_requests_total, ha_tool_calls_total, ha_agent_runs_total
curl -H 'X-API-Key: …' localhost:8080/v1/audit/verify
```

**Scaling.** One process on SQLite by design. To scale: replace `Store` with a Postgres
implementation (the class is the seam), move the rate limiter to Redis, then run N replicas. Do not
point multiple workers at one SQLite file.

**Failure behaviour.**
- LLM down → the run returns `failed` with a clear message; the fallback is the deterministic
  planner (`HA_LLM_PROVIDER=deterministic`).
- Approval expired → `invalid_request`, and the approval is marked `expired`.
- Budget exhausted → `budget_exceeded` with the partial trace, never a silent truncation.

`docs/RUNBOOK.md` has the deploy/rotate/incident procedures.

---

## Tests and evaluation

```bash
make test    # 109 tests: scoring, policy, tools, agent loop, API, MCP, FHIR
make eval    # 13 golden cases, writes eval/latest-report.md
```

The evaluation harness runs the offline planner, so it can gate CI with no key and no cost. For
each case it asserts the tool chosen, facts in the answer, and terminal status — the same contract
you would re-check after changing a prompt or a tool description.

---

## Repository layout

```
src/hospital_agent/
  agent/          loop, prompts (bounded by steps, tokens, wall clock)
  llm/            planner protocol, OpenAI-compatible client, deterministic planner
  tools/          registry (the choke point) + 14 tools + explainable scoring
  policy/         JSON rules + engine
  fhir/           R4 client, mapping, ingest, appointment write-back
  api/            FastAPI app, auth, rate limiting
  audit.py        hash chain
  redact.py       PHI masking
  store.py        SQLite schema, seed, repositories
mcp_server/       bridge (shared), SDK server, stdlib fallback, __main__
tests/            109 tests
eval/             cases.json, run_eval.py, latest-report.md
docs/             ARCHITECTURE.md, RUNBOOK.md, FHIR.md
```

---

## Honest limitations (read before production)

- **The seed is synthetic.** `store.py` seeds 44 fake patients. A FHIR R4 read path exists
  (`docs/FHIR.md`), but there is no HL7v2, no bulk `$export`, no incremental `_lastUpdated` sync and
  no propagation of deletions or merges. Treat the integration as a working read adapter, not a
  finished interface: point it at a test server before any real one.
- **Scores are rules, not validated models.** The weights are defensible priors, not trained on
  your population. Treat them as decision support and recalibrate against local outcomes.
- **The agent is not a clinician.** Prompts and tools keep it administrative; this is not a
  safeguard by itself, which is why write actions require human approval.
- **In-memory rate limiting and a single SQLite writer** are single-node answers.
- **Approval identity is authentication-strong only.** "A different person" is enforced by
  principal id; binding it to a real second factor is an integration decision.
- **Audit stores fingerprints, not payloads.** If you need full request reconstruction for
  compliance, add an encrypted evidence store — do not put PHI in the audit table.

---

## License

Proprietary. Synthetic data; no PHI. Not a medical device and not clinical decision support.
