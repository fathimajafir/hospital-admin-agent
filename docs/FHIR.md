# FHIR R4 integration

The agent can be fed from a real FHIR server instead of the synthetic seed. The
integration is deliberately narrow: four read resources, one write resource,
no modelling layer you have to learn.

```
fhir/client.py     HTTP only: search (with paging), read, create, metadata
fhir/mapping.py    pure functions, FHIR resource -> internal row, no I/O
fhir/ingest.py     pull, map, upsert, report; writes fhir_links
fhir/writeback.py  the one outbound path: Appointment, post-approval
```

## Configure

```bash
HA_FHIR_BASE_URL=https://fhir.example.org/r4
HA_FHIR_TOKEN=...                 # secret; environment only
HA_FHIR_MRN_SYSTEM=http://hospital.example.org/mrn
HA_FHIR_WRITE_ENABLED=false       # turn on only after a read-only pilot
```

`HA_FHIR_PAGE_SIZE` (100) and `HA_FHIR_MAX_PAGES` (20) bound every search. A sync
that would need more pages fails loudly rather than running forever.

## Run

```bash
# connectivity and what the server actually advertises
ha-agent fhir check --base-url https://hapi.fhir.org/baseR4

# dry run first: counts what would change, writes nothing
ha-agent fhir sync --from https://fhir.example.org/r4 --dry-run

# real pull
ha-agent fhir sync --from https://fhir.example.org/r4

# a Bundle from a bulk export or a test fixture
ha-agent fhir sync --from tests/fixtures/fhir/sample_bundle.json

# which FHIR resource became which local row
ha-agent fhir links --limit 20
```

Put the URL form on a schedule (cron, a Kubernetes CronJob). The job is
idempotent, so a nightly full pull is the expected mode today.

## What is mapped

| FHIR R4 | Local | Fields taken | Fields deliberately kept local |
|---|---|---|---|
| `Patient` | `patients` | name, birthDate, telecom, address, identifier (MRN) | `risk_score`, `documents`, `allergies`, `primary_care` |
| `Coverage` | `patients.payer` | `payor` label | — |
| `Appointment` | `appointments` | status, start, serviceType, practitioner display, appointmentType | `lead_days`*, `prior_no_shows`, `distance_km` |
| `Claim` | `claims` | status, total/net, item `productOrService`, diagnosis, `preAuthRef`, created | — |

\* `lead_days` is recomputed from `created` → `start` when the resource has a
`created` timestamp, otherwise left as-is.

On a re-sync, only the columns FHIR owns are updated. A `risk_score` you tuned
locally, a document checklist, a no-show count, a travel distance — none of them
are overwritten by a nightly pull. That is enforced in `Store.upsert_*`, not in
the mapping, so it holds for any caller.

### Identity

The external identity is the FHIR logical id, stored in `fhir_links`:

```
(source, resource_type, resource_id) -> internal_id
```

The internal patient id is the MRN identifier value when one is available
(matched by `HA_FHIR_MRN_SYSTEM`, else any identifier typed `MR`), otherwise the
FHIR logical id. `fhir_links` is what makes that reversible, and what lets an
Appointment resolve its patient when the patient is keyed by MRN.

### Status translation

| FHIR Appointment | Local |
|---|---|
| booked, pending, waitlist, arrived, checked-in | `scheduled` |
| fulfilled | `completed` |
| cancelled | `cancelled` |
| noshow | `no_show` |
| entered-in-error, anything else | **skipped**, with a warning |

| FHIR Claim | Local |
|---|---|
| active | `submitted` |
| draft | `draft` |
| cancelled | `void` |
| anything else | **skipped**, with a warning |

## The rule that shapes the mapping

**If FHIR does not say it, the resource is skipped, not defaulted.**

A Patient with no `birthDate` or no usable `name` is not loaded; the sync report
names the resource and the reason. A Claim whose patient is not in the store is
skipped. The alternative — inventing a date of birth, or attaching a claim to
the wrong patient because the ids looked similar — produces a database that
looks fine and is wrong, which is worse than a short report.

The single documented exception is `distance_km`, which has no FHIR equivalent.
An ingested appointment gets `0.0` and a warning saying the no-show model will
score it as if the patient lives next door. Fix it by joining a geocoded address
feed, not by silently guessing.

## Write-back

`HA_FHIR_WRITE_ENABLED=true` adds one outbound call: `schedule_appointment`
creates a FHIR `Appointment` after human approval.

Order is FHIR first, then local:

1. policy engine requires approval for `schedule_appointment`;
2. approval is granted, handler runs;
3. `POST /Appointment` — if the server rejects it, the call fails with 502,
   **no local row exists**, and the approval stays executable for a retry;
4. local row written, then `fhir_links` records the mapping.

The reverse order would leave a booked slot locally and nothing in the EHR when
the server is down. Divergence is the failure mode that stops staff trusting the
tool, so it is excluded by construction.

No `end` time is sent: the local schedule does not model visit duration, and a
guessed 30 minutes is indistinguishable from a real booking at the other end.

## Not covered

Be explicit about these before promising an interface:

- **Deletions and merges are not propagated.** A patient removed or merged in
  the EHR stays in the local table. There is no tombstone logic yet.
- **No incremental sync.** Every run pulls the full result set; `_lastUpdated`
  filtering is not implemented. Fine at 44 patients, not at 400k — add
  `_lastUpdated=gt<watermark>` and persist the watermark before using this on a
  real population.
- **No `$export` (bulk data), no Subscriptions, no HL7v2/MLLP.** The client is
  REST search + create only.
- **`Encounter` / `Location` are not mapped to `beds`.** Bed state is currently
  owned locally by the capacity tools. Mapping it needs a decision about who
  wins during a divergence, which is a ward-operations question, not a parsing
  one.
- **Clinical resources (`AllergyIntolerance`, `Condition`, `Observation`,
  `MedicationRequest`) are not ingested.** The `allergies` field stays whatever
  the seed or a human put there. This matters if you plan to surface clinical
  decision support; today the agent is administrative.
- **No validation against US Core / your server's CapabilityStatement.** The
  client does not check profiles, so a non-conformant server will be mapped
  best-effort and the gaps will show up as skips in the report.

## Operating notes

- The sync report contains resource ids and skip reasons, never clinical text or
  names. Safe to log and to ship to a monitoring system.
- Every sync writes one `fhir.sync` audit row (counts, duration, source, outcome)
  into the hash chain. Tokens are never recorded.
- `401/403` from the server means the token or its scopes are wrong; the error
  carries the `OperationOutcome` diagnostics, not the credential.
- The client refuses a paging `next` link that points at a different origin. A
  server that does that is either misconfigured or hostile; following it would
  send your bearer token to that host.

## Tests

`tests/test_fhir_*.py` cover mapping (including the refusals), the client against
`httpx.MockTransport` (paging, cross-origin link, OperationOutcome errors),
ingest idempotency and field preservation, and the write-back ordering. The
fixture in `tests/fixtures/fhir/sample_bundle.json` includes the awkward cases on
purpose: a patient with no birth date, one with no MRN, a claim for an unknown
patient, an appointment in `entered-in-error`.
