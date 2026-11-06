# Architecture and design decisions

## 1. The registry is the whole design

Every capability is a `ToolSpec` (name, description, Pydantic args model, handler). Every entry
point — HTTP, MCP, the agent loop, the CLI — calls `ToolRegistry.call`.

That single funnel is what makes the security claims true:

```
call(name, args, ctx)
  ├─ policy.enforce(principal, name, purpose)     → 403 or decision
  ├─ approval gate (if required and no approval)  → 202 + pending approval
  ├─ args_model.model_validate(args)              → 422 with field errors
  ├─ idempotency lookup (mutating tools)          → replay returns the first result
  ├─ handler(ctx, args)                           → domain logic
  ├─ redact_record(...)                           → PHI masking
  └─ audit.record(...)                            → hash-chained evidence
```

If you add a capability anywhere else, you have added a hole. This is the one rule to enforce in
review.

**Tradeoff:** a little indirection (handlers receive a validated model, not a dict) in exchange for
the guarantee that no caller can skip policy.

---

## 2. Agent loop: three budgets, resumable state

```
plan ──▶ tool? ──▶ execute ──▶ observation ──┐
  ▲                                          │
  └──────────────────────────────────────────┘
     bounded by: steps | tokens | wall clock
```

- **Steps** (`HA_MAX_STEPS`, hard cap 20) — agents fail by looping, not by being wrong once.
- **Tokens** — checked before each plan call, so a runaway conversation stops early.
- **Wall clock** — protects the request from a slow provider.

A step that fails with a recoverable error (unknown tool, policy denial, bad args) is fed back as an
observation so the planner can adapt **once**. A hard failure (provider down) terminates the run
with `failed`.

**Approvals.** A write tool raises `ApprovalRequired`, which carries the approval id. The loop
persists `messages`, `steps`, `usage` and returns `awaiting_approval`. `resume()` reloads that
state, executes the approved tool, appends the observation, and continues the same conversation.
The run id is stable across the pause, so the audit trail is one linear story.

**Tradeoff:** a paused run holds its conversation in the `runs` table. With a real model this
includes the prompt context. It is redaction-friendly (no PHI in tool observations beyond what the
caller could already see) but it is still stored state — treat the `runs` table as sensitive.

---

## 3. Planner protocol: JSON decisions, not provider tool-calls

The planner returns exactly one of:

```json
{"tool": "capacity_snapshot", "args": {"ward": "ED"}}
{"final": "..."}
{"question": "..."}
```

Why not native function calling?

| | JSON decision | Provider tool-calls |
|---|---|---|
| Portability | any `/chat/completions` | per-provider object mapping |
| Testability | assert on one JSON object | mock provider response shapes |
| Debugging | invalid output is a schema error you can log | failure modes hide in adapters |
| Streaming | step-level (which is what matters) | token-level |

The cost is that the prompt must carry the JSON Schema. `build_system_prompt` injects it **verbatim
from the registry**, so the prompt and the validator cannot drift.

---

## 4. Deterministic planner: not a toy

`DeterministicPlanner` is a first-class mode, not a stub:

- CI runs the full agent loop with no key, no network, no cost.
- Evaluation cases (`eval/cases.json`) assert routing and answer content.
- If the provider is unreachable, switching `HA_LLM_PROVIDER=deterministic` keeps the service
  useful — less fluent, equally correct on facts.

It routes on intent keywords, extracts ids (`P0005`, `C0003`, ward codes, CPT, ISO datetimes), and
renders each tool result through a per-tool template. It never invents a number because it never
generates prose from parametric memory.

---

## 5. Scoring: rules with reasons

All scores are deterministic functions in `tools/scoring.py`:

| Score | Form | Why |
|---|---|---|
| No-show probability | logistic over 6 features, contributions returned | auditable, monotonic, no training data needed |
| Discharge readiness | `1 − Σ barrier weights`, capped | barriers compound; each has an owner |
| Claim denial risk | rule findings, severity-weighted | each finding carries the fix |
| Capacity pressure | weighted occupancy + boarders + ED wait | matches how flow teams triage |

**Why not a trained model?** Because a model you cannot explain will not be trusted by a charge
nurse, and because you do not have labelled data until you have run this for a while. The functions
are the calibration harness: log predictions vs outcomes, then replace the weights (or the whole
function) with a model that beats them.

**The seam is one function.** Every tool calls these, so swapping in a model service is a
contained change.

---

## 6. Data model

SQLite, one writer, WAL. The `Store` class is deliberately a repository interface — tools never
write SQL.

```
patients ──┬── beds (patient_id)
           ├── appointments
           ├── discharges (barriers as JSON)
           └── claims
staff_shifts   ward × shift × role
approvals      one row per gated action (status: pending→approved→executed)
runs           full agent state (messages/steps/usage) for resume + debugging
audit          append-only, hash-chained
idempotency    key → first response
```

**Why SQLite?** It makes the whole thing runnable with zero infrastructure and keeps the demo
honest. It is also a hard scaling ceiling: one writer, no horizontal reads. The migration path is
`Store` → Postgres, `RateLimiter` → Redis, then replicas. Nothing above the store changes.

**Seed data** is 44 synthetic patients across 6 wards (54 beds), 14 appointments, 12 claims,
10 discharge plans and a roster with two deliberate night-shift gaps. It is generated with fixed
seeds, so every run and every test sees the same world.

---

## 7. Security decisions worth arguing about

**Deny-by-default.** Unknown tools require the `admin` permission. Adding a tool without a rule does
not silently expose it to everyone.

**Purpose of use is a real gate.** Not a log field. It must be in `PURPOSES` *and* granted to the
principal *and* passed by the caller. This stops "just add purpose=treatment" from being a bypass.

**Approvals are single-use.** After execution the status becomes `executed`. Replaying an approval
id fails. This closes the most common approval-system bug: the id becomes a permanent capability.

**Requester ≠ approver.** Enforced on principal id in the API layer. It is authentication-strength
only — see limitations.

**Audit stores fingerprints.** `args_hash` (SHA-256, truncated) proves *what* was called without
copying PHI into a second table. If compliance needs payload reconstruction, add an encrypted
evidence store; do not weaken this one.

**Redaction is layered.** Policy decides *whether* PHI may be returned; `redact_record` protects
against misconfiguration and covers logs/errors. Both are tested.

---

## 8. Adding a tool (the 10-minute version)

1. Create `ArgsModel(BaseModel)` with `Field(description=...)` — the description is the LLM's and
   the MCP client's only documentation.
2. Write `handler(ctx: ToolContext, args: ArgsModel) -> dict`. Read via `ctx.store`, never open a
   connection. Raise `KeyError` for not-found, `ValueError` for business-rule violations.
3. Add a `ToolSpec(...)` and append it to the module's `*_tools()` list.
4. Add a rule in `policy/rules.default.json` (permissions, `requires_approval`, `purpose_required`).
5. If it is a write: set `mutates=True`, and make the handler safe to call twice.
6. Add a redaction-safe test and one evaluation case.

No changes needed in the API, the MCP server, the CLI or the agent.

---

## 9. What I would build next

1. **FHIR/HL7 ingestion** — replace seeded tables with a read replica; keep `Store` as the adapter.
2. **Outcome logging** — record actual no-shows, denials and discharge times; then calibrate.
3. **Encrypted evidence store** — for full audit reconstruction under a separate key.
4. **Multi-tenant isolation** — `tenant` is on every principal and audit row but the store is
   single-tenant today; that is the first thing to fix before serving two hospitals.
5. **Approval second factor** — bind the decision to an SSO assertion or a signed device key.
6. **Postgres + Redis** — the only changes needed for horizontal scale.
