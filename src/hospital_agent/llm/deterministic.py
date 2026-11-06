"""A planner that needs no model.

Two jobs:

1. Make the agent runnable and testable with zero network, zero keys and zero
   cost - which is also what CI uses.
2. Act as the fallback when the LLM provider is down, so the service degrades to
   "useful but less fluent" instead of "unavailable".

It routes on intent, extracts identifiers, and renders the tool result into
plain operational English. It is deliberately predictable: same input, same
answer, every time.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

from .base import Decision, Final, ToolCall

PATIENT_RE = re.compile(r"\bP\d{4}\b")
CLAIM_RE = re.compile(r"\bC\d{4}\b")
WARD_RE = re.compile(r"\b(ED|ICU|MED-[AB]|SURG|PEDS)\b")
CPT_RE = re.compile(r"\b\d{5}\b")
DATETIME_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")


class DeterministicPlanner:
    name = "deterministic"

    def plan(
        self,
        *,
        task: str,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]],
        remaining_steps: int,
    ) -> Decision:
        observations = [m for m in messages if m.get("role") == "tool"]
        if observations:
            return Final(_summarise(observations[-1]["content"]))
        return self._route(task, {t["name"] for t in tools})

    # -- routing -----------------------------------------------------------
    def _route(self, task: str, available: set[str]) -> Decision:
        text = task.lower()
        patient = _first(PATIENT_RE, task)
        claim = _first(CLAIM_RE, task)
        ward = _first(WARD_RE, task)
        cpt = _first(CPT_RE, task)

        plan: list[tuple[bool, str, dict[str, Any]]] = [
            (
                any(k in text for k in ("prior auth", "prior-auth", "authorization", "authorisation")) and bool(cpt),
                "build_prior_auth_packet",
                {"patient_id": patient, "cpt": cpt},
            ),
            (
                any(k in text for k in ("discharge", "go home", "ready to leave")),
                "discharge_board",
                {"ward": ward},
            ),
            (
                any(k in text for k in ("staff", "roster", "short", "nurse", "coverage")),
                "staffing_gaps",
                {"ward": ward},
            ),
            (
                any(k in text for k in ("no-show", "no show", "no shows", "miss the appointment", "attendance")),
                "appointment_risk",
                {"patient_id": patient} if patient else {"appt_type": "follow_up"},
            ),
            (
                any(k in text for k in ("book", "schedule", "reserve"))
                and patient
                and _first(DATETIME_RE, task),
                "schedule_appointment",
                {"patient_id": patient, "dept": _dept(task) or "Primary Care", "provider": "Dr. Rao",
                 "start_ts": _first(DATETIME_RE, task)},
            ),
            (
                any(k in text for k in ("slot", "availability", "opening", "book", "schedule", "appointment")),
                "find_appointment_slots",
                {"dept": _dept(task) or "Primary Care", "patient_id": patient},
            ),
            (
                any(k in text for k in ("claim", "denial", "denied", "billing", "revenue", "payer")),
                "claim_scrub" if claim else "billing_summary",
                {"claim_id": claim} if claim else {},
            ),
            (
                any(k in text for k in ("quality", "kpi", "indicator", "benchmark")),
                "quality_indicators",
                {},
            ),
            (
                any(k in text for k in ("digest", "brief", "morning", "handover", "round", "summary", "today")),
                "ops_digest",
                {"ward": ward},
            ),
            (
                any(k in text for k in ("capacity", "bed", "occupancy", "boarder", "surge", "flow")),
                "capacity_snapshot",
                {"ward": ward},
            ),
            (
                bool(patient) and any(k in text for k in ("patient", "who is", "look up", "record")),
                "patient_get",
                {"patient_id": patient},
            ),
        ]

        for matches, tool, args in plan:
            if matches and tool in available:
                clean = {k: v for k, v in args.items() if v not in (None, "")}
                return ToolCall(tool, clean)

        if "ops_digest" in available:
            return ToolCall("ops_digest", {"ward": ward} if ward else {})
        if "capacity_snapshot" in available:
            return ToolCall("capacity_snapshot", {"ward": ward} if ward else {})
        return Final(
            "I do not have a tool available for this request under your current permissions. "
            "Ask an administrator to review your role's access."
        )


# -- summarisation ---------------------------------------------------------
def _summarise(observation: str) -> str:
    try:
        payload = json.loads(observation)
    except json.JSONDecodeError:
        return observation[:2000]
    tool = payload.get("tool", "")
    if not payload.get("ok"):
        return f"{tool} did not complete: {payload.get('data')}"
    data = payload.get("data") or {}
    renderer: Callable[[dict[str, Any]], str] = _RENDERERS.get(tool, _generic)
    try:
        return renderer(data)
    except (KeyError, TypeError, ValueError):
        return _generic(data)


def _generic(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, default=str)[:2000]


def _capacity(data: dict[str, Any]) -> str:
    totals = data["totals"]
    pressure = data["pressure"]
    lines = [
        f"Occupancy is {totals['occupancy']:.0%} ({totals['occupied']}/{totals['beds']} beds), "
        f"{totals['boarders']} boarders. Hospital pressure {pressure['pressure']:.2f} ({pressure['band']})."
    ]
    tight = [w for w in data["wards"]][:3]
    for w in tight:
        extra = f", {w['boarders']} boarding" if w["boarders"] else ""
        ed = f", ED wait ~{w['ed_wait_hours']}h" if w.get("ed_wait_hours") else ""
        lines.append(f"- {w['ward']}: {w['occupancy']:.0%} ({w['occupied']}/{w['beds']}){extra}{ed} - {w.get('action', '')}")
    if data.get("escalation_required"):
        lines.append("Escalation threshold reached: notify the house supervisor now.")
    return "\n".join(lines)


def _discharge(data: dict[str, Any]) -> str:
    lines = [
        f"{data['count']} planned discharges: {data['ready_today']} ready today, "
        f"{data['at_risk']} at risk of missing their target date."
    ]
    for plan in data["plans"][:4]:
        blockers = ", ".join(b["barrier"].replace("_", " ") for b in plan["blocking"]) or "no blockers"
        lines.append(
            f"- {plan['patient_id']} ({plan['ward']}): readiness {plan['readiness']:.2f}, "
            f"target {plan['target_date']} - {blockers}"
        )
        if plan["next_actions"]:
            lines.append(f"  next: {plan['next_actions'][0]}")
    return "\n".join(lines)


def _staffing(data: dict[str, Any]) -> str:
    gaps = [s for s in data["shifts"] if s["severity"] != "covered"]
    if not gaps:
        return "All shifts meet the acuity-adjusted staffing requirement."
    critical = data["critical_shifts"]
    lines = [
        f"{critical} critical shift" + ("" if critical == 1 else "s")
        + f"; {len(gaps)} below requirement overall."
    ]
    for shift in gaps[:4]:
        lines.append(
            f"- {shift['ward']} {shift['shift']}: {shift['assigned_rn']} RN vs {shift['required_rn']} required "
            f"(gap {max(shift['gap_rn'], 0)}), {shift['assigned_md']} MD vs {shift['required_md']} - {shift['action']}"
        )
    return "\n".join(lines)


def _risk(data: dict[str, Any]) -> str:
    drivers = ", ".join(
        f"{d['factor'].replace('_', ' ')} ({d['contribution']:+.2f})" for d in data["drivers"][:3]
    ) or "no dominant driver"
    who = data.get("patient_id") or "this booking"
    return (
        f"No-show probability for {who} is {data['probability']:.2f} ({data['band']}). "
        f"Drivers: {drivers}. Recommendation: {data['recommendation']}."
        + (" Overbooking is advised." if data.get("overbook_recommended") else "")
    )


def _slots(data: dict[str, Any]) -> str:
    slots = data["slots"]
    if not slots:
        return f"No open slots found for {data['dept']} in the requested window."
    lines = [f"Top {len(slots)} of {data['total_considered']} slots for {data['dept']} (ranked by {data['ranking']}):"]
    for slot in slots:
        lines.append(
            f"- {slot['start_ts']} with {slot['provider']} "
            f"(expected attendance {slot['expected_attendance']:.0%}, value {slot['slot_value']:.2f})"
        )
    return "\n".join(lines)


def _booked(data: dict[str, Any]) -> str:
    appt = data["appointment"]
    return (
        f"Booked {appt['patient_id']} with {appt['provider']} in {appt['dept']} at {appt['start_ts']}. "
        f"No-show risk {data['no_show_risk']['probability']:.2f}; reminders: {', '.join(data['reminders'])}."
    )


def _claim(data: dict[str, Any]) -> str:
    lines = [
        f"Claim {data['claim_id']} (CPT {data['cpt']}, {data['payer']}, ${data['amount_usd']:,.2f}): "
        f"denial risk {data['denial_risk']:.2f} ({data['band']})."
    ]
    for finding in data["findings"][:4]:
        lines.append(f"- {finding['code']}: {finding['message']} -> {finding['fix']}")
    if not data["findings"]:
        lines.append("No scrub findings; safe to submit.")
    return "\n".join(lines)


def _billing(data: dict[str, Any]) -> str:
    lines = [
        f"{data['claims_reviewed']} claims reviewed: ${data['charges_usd']:,.0f} charges, "
        f"${data['at_risk_usd']:,.0f} at risk ({data['at_risk_share']:.0%})."
    ]
    for claim in data["priority_claims"][:3]:
        lines.append(
            f"- {claim['claim_id']} {claim['payer']} ${claim['amount_usd']:,.0f} "
            f"risk {claim['denial_risk']:.2f} ({claim['top_issue']})"
        )
    return "\n".join(lines)


def _prior_auth(data: dict[str, Any]) -> str:
    lines = [
        f"Prior authorisation for {data['procedure']} (CPT {data['cpt']}, {data['payer']}): "
        f"{'ready' if data['ready_to_submit'] else 'not ready'}, "
        f"estimated first-pass approval {data['estimated_first_pass_likelihood']:.0%}."
    ]
    if data["missing"]:
        lines.append("Missing: " + ", ".join(m.replace("_", " ") for m in data["missing"]))
    lines.extend(f"- {step}" for step in data["next_steps"][:3])
    return "\n".join(lines)


def _patient(data: dict[str, Any]) -> str:
    bed = f"bed {data['current_bed']} ({data['current_ward']})" if data.get("current_bed") else "not admitted"
    return (
        f"{data['name']} ({data['id']}, MRN {data['mrn']}), born {data['dob']}, payer {data['payer']}, "
        f"risk score {data['risk_score']:.2f}, {bed}."
    )


def _patient_search(data: dict[str, Any]) -> str:
    if not data["count"]:
        return f"No patients matched '{data['query']}'."
    lines = [f"{data['count']} match(es) for '{data['query']}':"]
    lines.extend(f"- {p['id']} {p['name']} (MRN {p['mrn']}, {p['payer']})" for p in data["results"][:6])
    return "\n".join(lines)


def _quality(data: dict[str, Any]) -> str:
    lines = ["Quality indicators:"]
    for item in data["indicators"]:
        lines.append(f"- {item['name'].replace('_', ' ')}: {item['value']} (benchmark {item['benchmark']}) - {item['status']}")
    if data["watching"]:
        lines.append("Off target: " + ", ".join(w.replace("_", " ") for w in data["watching"]))
    return "\n".join(lines)


def _digest(data: dict[str, Any]) -> str:
    lines = [data["headline"], "", "Top actions:"]
    for action in data["top_actions"]:
        lines.append(
            f"{action['priority']}. [{action['domain']}] {action['action']} "
            f"(owner: {action['owner']}; impact: {action['impact']})"
        )
    return "\n".join(lines)


_RENDERERS: dict[str, Callable[[dict[str, Any]], str]] = {
    "capacity_snapshot": _capacity,
    "discharge_board": _discharge,
    "staffing_gaps": _staffing,
    "appointment_risk": _risk,
    "find_appointment_slots": _slots,
    "schedule_appointment": _booked,
    "claim_scrub": _claim,
    "billing_summary": _billing,
    "build_prior_auth_packet": _prior_auth,
    "patient_get": _patient,
    "patient_search": _patient_search,
    "quality_indicators": _quality,
    "ops_digest": _digest,
}


def _first(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text)
    return match.group(0) if match else None


DEPARTMENTS = ["Cardiology", "Orthopedics", "Oncology", "Primary Care", "Neurology", "Imaging"]


def _dept(task: str) -> str | None:
    for dept in DEPARTMENTS:
        if dept.lower() in task.lower():
            return dept
    return None
