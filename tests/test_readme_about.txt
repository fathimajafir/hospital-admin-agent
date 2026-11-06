## What it is

A hospital **back-office agent**: you ask it an operational question in plain English, and it answers using 14 narrow tools that read (and occasionally write) hospital data. It is not an EHR, not a clinical assistant, and not a chatbot that improvises — every number it gives comes from a tool call that could be recomputed by hand.

## What it does

Five jobs, roughly one per department that gets buried in manual work:

| Job | Example question | What comes back |
|---|---|---|
| **Flow** | "How tight is the hospital right now?" | Occupancy, boarders, ED wait, a 0–1 pressure score per ward, the next action |
| **Discharge** | "What's blocking discharges today?" | Each blocked patient, the barrier, **who owns it**, and what unblocks it |
| **Scheduling** | "Book Mrs X into ortho, best slot" | No-show probability with drivers, slots ranked by expected attendance, booking behind an approval |
| **Revenue** | "Why will this claim be denied?" | Denial risk with a specific fix per finding; prior-auth packet with required vs missing documents |
| **Patient** | "Pull up P0007" | Record, PHI-masked unless the caller's role, purpose and permission all line up |

Plus the piece you just asked for: it can now pull `Patient`, `Coverage`, `Appointment` and `Claim` from a FHIR server instead of the 44 seeded fake patients.

## How it does it

Think of it as a loop with a very strict doorman.

1. **Understand → pick a tool.** An LLM (or a deterministic rule-based planner, which is the default) reads the question, picks a tool, and fills in arguments. It cannot run SQL or reach the database.
2. **The tool registry is the only door.** Every call passes through one object that checks permission, purpose of use, approval requirement, redaction and audit. A new tool cannot accidentally bypass policy, and neither can a new front-end.
3. **Scores are formulas you can argue with.** No-show risk is a logistic function of lead time, prior no-shows, payer, distance and visit type — and it returns the per-factor contribution, not just "0.38". That's why a coordinator can say "the distance is wrong" and fix an input rather than distrust the output.
4. **Writes pause, they don't just happen.** Booking a bed or an appointment returns `awaiting_approval` with an id. A *different* principal approves, then the run resumes from where it stopped. That's a state machine, not a dead end.
5. **Everything is recorded.** Hash-chained audit of every call, with argument fingerprints rather than raw PHI.

The honest headline: **the intelligence is in the models and the workflow, not in the language model.** The LLM is a router and a summariser. Remove it (`HA_LLM_PROVIDER=deterministic`) and every tool still works — which is also how the test suite and the 13-case eval run with no API key.

## How it differs from what's out there

Worth being precise, because "hospital AI" covers wildly different things:

**vs. the EHR's built-in reports (Epic/Cerner workqueues, dashboards).** Those are authoritative and system-of-record, but they're built around *their* workflow and they don't rank work for you or explain a score. This agent is the opposite trade-off: narrow, explainable, and composable into a chat, an MCP client, or an API — but it needs the EHR to be useful, which is why the FHIR adapter matters more than any prompt tweak.

**vs. an analytics dashboard.** A dashboard shows you occupancy is 92%. This tells you occupancy is 92%, two discharges are blocked on imaging, and the person to call is radiology. The difference is *next action with an owner*, not a chart.

**vs. a general LLM chatbot pointed at a database.** Two structural differences: it has no free-form data access (so it cannot hallucinate a patient record), and its write actions are gated and audited rather than "please confirm". Ask a generic chatbot to book an appointment and you get a plausible-sounding message; here you get an approval record.

**vs. RPA / rule engines.** Rules can't rank, explain, or summarise. This uses rules *inside* the scoring and policy layers — which is why it behaves the same on every run — and puts language on top for routing and narration.

**vs. clinical AI (sepsis prediction, imaging, ambient scribing).** Different domain entirely. This one deliberately stays administrative. It does not diagnose, and the code says so.

## What it isn't

- **Not production-ready for a real hospital.** Synthetic data by default, single SQLite writer, in-memory rate limiting, scores calibrated as priors rather than validated against your outcomes.
- **Not a complete EHR interface.** FHIR read for four resources, one write path, no deletions, no incremental sync yet.
- **Not self-learning.** It won't get smarter from use; it gets *tuned* when someone recalibrates the weights against your no-show and denial outcomes. That's a feature for a regulated setting, and a limitation if you expected otherwise.

If you want the one-line version: **it's a small, auditable operations layer that turns hospital data into ranked, explained next actions — and refuses to do anything irreversible without a second human.**

Want me to walk through one concrete request end to end (say, the morning digest) so you can see the actual tool calls and output?