"""Deterministic, explainable scoring.

Every score returns its drivers alongside the number. A clinician or an auditor
can ask "why 0.63?" and get an answer that does not depend on a model's mood.
Weights are starting points to be tuned against local outcome data.
"""

from __future__ import annotations

import math
from typing import Any

HIGH_RISK_PAYERS = {"SelfPay", "Medicaid"}

BARRIER_WEIGHTS: dict[str, float] = {
    "awaiting_imaging": 0.28,
    "awaiting_labs": 0.18,
    "pt_eval_pending": 0.22,
    "social_work_clearance": 0.30,
    "med_recs_reconciliation": 0.12,
    "transport_arranged": 0.26,
    "insurance_authorization": 0.20,
}


def _logistic(z: float) -> float:
    return 1.0 / (1.0 + math.exp(-z))


def no_show_probability(
    *,
    lead_days: int,
    prior_no_shows: int,
    appt_type: str,
    payer: str,
    distance_km: float,
    has_contact: bool = True,
) -> dict[str, Any]:
    """Logistic model over scheduling features, with per-feature contributions."""
    terms: list[tuple[str, float]] = [
        ("baseline", -2.30),
        ("lead_days", 0.045 * min(max(lead_days, 0), 90)),
        ("prior_no_shows", 0.55 * min(max(prior_no_shows, 0), 4)),
        ("new_patient", 0.40 if appt_type == "new" else 0.0),
        ("payer_risk", 0.35 if payer in HIGH_RISK_PAYERS else 0.0),
        ("distance", 0.012 * min(max(distance_km, 0), 60)),
        ("no_contact", 0.45 if not has_contact else 0.0),
    ]
    z = sum(value for _, value in terms)
    probability = round(_logistic(z), 3)

    drivers = [
        {"factor": name, "contribution": round(value, 3)}
        for name, value in terms
        if name != "baseline" and abs(value) >= 0.01
    ]
    drivers.sort(key=lambda d: -d["contribution"])

    return {
        "probability": probability,
        "band": _risk_band(probability),
        "drivers": drivers,
        "recommendation": _no_show_action(probability, lead_days),
        "model": "logistic-v1",
    }


def _risk_band(p: float) -> str:
    if p >= 0.6:
        return "high"
    if p >= 0.35:
        return "medium"
    if p >= 0.15:
        return "low"
    return "very_low"


def _no_show_action(p: float, lead_days: int) -> str:
    if p >= 0.6:
        return "offer earlier slot, confirm 72h and 24h ahead, and hold a standby patient"
    if p >= 0.35:
        return "send 24h reminder and require confirmation; enable standby fill"
    if lead_days > 21:
        return "confirm at 7 days; consider shortening the booking horizon"
    return "standard reminders are sufficient"


def discharge_readiness(barriers: list[str]) -> dict[str, Any]:
    """1.0 = nothing blocking. Multiple barriers compound, never sum linearly."""
    if not barriers:
        return {"readiness": 1.0, "band": "ready", "blocking": [], "next_actions": ["schedule transport and finalise paperwork"]}

    weight = sum(BARRIER_WEIGHTS.get(b, 0.2) for b in barriers)
    readiness = round(max(0.0, 1.0 - min(weight, 0.95)), 2)

    blocking = [
        {"barrier": b, "weight": BARRIER_WEIGHTS.get(b, 0.2), "owner": _barrier_owner(b)}
        for b in barriers
    ]
    blocking.sort(key=lambda item: -item["weight"])

    return {
        "readiness": readiness,
        "band": "ready" if readiness >= 0.85 else "nearly_ready" if readiness >= 0.6 else "blocked",
        "blocking": blocking,
        "next_actions": [_barrier_action(b["barrier"]) for b in blocking[:3]],
    }


def _barrier_owner(barrier: str) -> str:
    return {
        "awaiting_imaging": "radiology",
        "awaiting_labs": "laboratory",
        "pt_eval_pending": "physical_therapy",
        "social_work_clearance": "care_management",
        "med_recs_reconciliation": "pharmacy",
        "transport_arranged": "patient_transport",
        "insurance_authorization": "utilization_management",
    }.get(barrier, "care_team")


def _barrier_action(barrier: str) -> str:
    return {
        "awaiting_imaging": "expedite read or confirm the result is not discharge-critical",
        "awaiting_labs": "check whether the pending panel is required before discharge",
        "pt_eval_pending": "book PT evaluation or document a home-safety plan",
        "social_work_clearance": "assign a case manager and confirm the post-acute plan",
        "med_recs_reconciliation": "complete pharmacist reconciliation",
        "transport_arranged": "book transport with a confirmed pickup window",
        "insurance_authorization": "obtain authorisation or start the appeal packet",
    }.get(barrier, f"resolve {barrier}")


#: CPT families that need prior authorisation, with their required documents.
PRIOR_AUTH_RULES: dict[str, dict[str, Any]] = {
    "27447": {"label": "Total knee arthroplasty", "docs": ["referral", "prior_auth", "imaging_report", "conservative_tx_trial"]},
    "70551": {"label": "MRI brain", "docs": ["referral", "prior_auth", "clinical_notes"]},
    "73721": {"label": "MRI knee", "docs": ["referral", "prior_auth", "conservative_tx_trial"]},
    "43239": {"label": "Upper GI endoscopy", "docs": ["referral", "prior_auth", "clinical_notes"]},
    "20610": {"label": "Joint injection", "docs": ["referral", "clinical_notes"]},
    "97110": {"label": "Therapeutic exercise", "docs": ["referral", "plan_of_care"]},
}

#: CPT -> compatible ICD-10 prefixes. Anything else is a coding mismatch.
CPT_ICD_COMPAT: dict[str, tuple[str, ...]] = {
    "99213": ("I", "E", "J", "M", "Z", "R"),
    "99214": ("I", "E", "J", "M", "Z", "R"),
    "99215": ("I", "E", "J", "M", "Z", "R"),
    "70551": ("R", "G", "I", "C"),
    "27447": ("M",),
    "93000": ("I", "R", "Z"),
    "97110": ("M", "S", "G"),
    "73721": ("M", "S"),
    "43239": ("K",),
    "36415": ("Z", "R"),
    "87635": ("U", "J", "Z"),
    "20610": ("M",),
}

AUTH_THRESHOLD_CENTS = 50_000


def claim_denial_risk(claim: dict[str, Any]) -> dict[str, Any]:
    """Rule-based scrub. Each finding carries the fix, not just the flag."""
    findings: list[dict[str, Any]] = []
    cpt = str(claim.get("cpt", ""))
    icd = str(claim.get("icd10", ""))
    amount = int(claim.get("amount_cents", 0))

    compatible = CPT_ICD_COMPAT.get(cpt)
    if compatible and icd and not icd.upper().startswith(tuple(compatible)):
        findings.append({
            "code": "CPT_ICD_MISMATCH",
            "severity": "high",
            "message": f"CPT {cpt} is not supported by diagnosis {icd}",
            "fix": "correct the diagnosis code or change the procedure code before submission",
        })

    if amount >= AUTH_THRESHOLD_CENTS and not claim.get("auth_on_file"):
        findings.append({
            "code": "MISSING_AUTH",
            "severity": "high",
            "message": f"claim of ${amount / 100:,.2f} has no authorisation on file",
            "fix": "obtain prior authorisation; retrospective auth succeeds far less often",
        })

    if cpt in PRIOR_AUTH_RULES and not claim.get("auth_on_file"):
        findings.append({
            "code": "AUTH_REQUIRED_BY_POLICY",
            "severity": "high",
            "message": f"{PRIOR_AUTH_RULES[cpt]['label']} requires prior authorisation",
            "fix": "build the prior-auth packet and submit before the claim",
        })

    if amount <= 0:
        findings.append({
            "code": "ZERO_AMOUNT",
            "severity": "medium",
            "message": "claim amount is zero",
            "fix": "verify the charge capture for this encounter",
        })

    if claim.get("payer") == "SelfPay":
        findings.append({
            "code": "SELF_PAY",
            "severity": "low",
            "message": "self-pay claim: reimbursement is not guaranteed",
            "fix": "confirm financial counselling was offered",
        })

    severity_weight = {"high": 0.25, "medium": 0.12, "low": 0.05}
    risk = round(min(0.05 + sum(severity_weight.get(f["severity"], 0.05) for f in findings), 0.95), 2)

    return {
        "denial_risk": risk,
        "band": "high" if risk >= 0.5 else "medium" if risk >= 0.25 else "low",
        "findings": findings,
        "cleared": not findings,
        "model": "rules-v1",
    }


def capacity_pressure(*, occupancy: float, boarders: int, ed_wait_hours: float) -> dict[str, Any]:
    score = (
        0.55 * min(occupancy, 1.0)
        + 0.25 * min(boarders / 6.0, 1.0)
        + 0.20 * min(ed_wait_hours / 8.0, 1.0)
    )
    score = round(min(score, 1.0), 2)
    band = "critical" if score >= 0.85 else "high" if score >= 0.7 else "elevated" if score >= 0.5 else "normal"
    return {"pressure": score, "band": band}
