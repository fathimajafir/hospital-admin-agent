"""Revenue cycle: claim scrubbing, prior authorisation, denial exposure."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ..models import utcnow
from .registry import ToolContext, ToolSpec
from .scoring import PRIOR_AUTH_RULES, claim_denial_risk


class ClaimArgs(BaseModel):
    claim_id: str = Field(..., description="Claim identifier, e.g. C0003.")


def claim_scrub(ctx: ToolContext, args: ClaimArgs) -> dict[str, Any]:
    claim = ctx.store.get_claim(args.claim_id)
    if claim is None:
        raise KeyError(f"claim {args.claim_id} not found")
    assessment = claim_denial_risk(claim)
    return {
        "claim_id": claim["id"],
        "cpt": claim["cpt"],
        "icd10": claim["icd10"],
        "payer": claim["payer"],
        "amount_usd": round(claim["amount_cents"] / 100, 2),
        "auth_on_file": bool(claim["auth_on_file"]),
        **assessment,
        "recommended_action": (
            "hold and fix the flagged items before submission"
            if assessment["band"] == "high"
            else "submit; monitor for a payer query"
        ),
    }


class SummaryArgs(BaseModel):
    payer: str | None = Field(None, description="Restrict to one payer.")
    limit: int = Field(10, ge=1, le=50, description="How many high-risk claims to list.")


def billing_summary(ctx: ToolContext, args: SummaryArgs) -> dict[str, Any]:
    claims = ctx.store.claims(payer=args.payer)
    if not claims:
        raise ValueError("no claims found for that filter")

    by_payer: dict[str, dict[str, float]] = {}
    at_risk: list[dict[str, Any]] = []

    for claim in claims:
        assessment = claim_denial_risk(claim)
        amount = claim["amount_cents"] / 100
        bucket = by_payer.setdefault(claim["payer"], {"claims": 0, "charges_usd": 0.0, "at_risk_usd": 0.0})
        bucket["claims"] += 1
        bucket["charges_usd"] += amount
        bucket["at_risk_usd"] += amount * assessment["denial_risk"]
        if assessment["band"] in {"high", "medium"}:
            at_risk.append(
                {
                    "claim_id": claim["id"],
                    "payer": claim["payer"],
                    "amount_usd": round(amount, 2),
                    "denial_risk": assessment["denial_risk"],
                    "top_issue": assessment["findings"][0]["code"] if assessment["findings"] else None,
                }
            )

    at_risk.sort(key=lambda c: -c["amount_usd"] * c["denial_risk"])
    charges = sum(v["charges_usd"] for v in by_payer.values())
    risk_dollars = sum(v["at_risk_usd"] for v in by_payer.values())

    return {
        "generated_at": utcnow(),
        "claims_reviewed": len(claims),
        "charges_usd": round(charges, 2),
        "at_risk_usd": round(risk_dollars, 2),
        "at_risk_share": round(risk_dollars / charges, 2) if charges else 0.0,
        "by_payer": [
            {
                "payer": payer,
                **{k: round(v, 2) for k, v in values.items()},
                "at_risk_share": round(values["at_risk_usd"] / values["charges_usd"], 2) if values["charges_usd"] else 0.0,
            }
            for payer, values in sorted(by_payer.items(), key=lambda kv: -kv[1]["at_risk_usd"])
        ],
        "priority_claims": at_risk[: args.limit],
    }


class PriorAuthArgs(BaseModel):
    patient_id: str = Field(..., description="Patient the authorisation is for.")
    cpt: str = Field(..., description="Procedure code, e.g. 27447.")


def build_prior_auth_packet(ctx: ToolContext, args: PriorAuthArgs) -> dict[str, Any]:
    patient = ctx.store.get_patient(args.patient_id)
    if patient is None:
        raise KeyError(f"patient {args.patient_id} not found")

    rule = PRIOR_AUTH_RULES.get(args.cpt)
    label = rule["label"] if rule else f"procedure {args.cpt}"
    required = list(rule["docs"]) if rule else ["referral", "prior_auth", "clinical_notes"]
    on_file = set(patient.get("documents") or [])
    present = [doc for doc in required if doc in on_file]
    missing = [doc for doc in required if doc not in on_file]

    risk = 0.15
    if not rule:
        risk += 0.15  # no policy rule on file -> payer will ask questions
    risk += 0.12 * len(missing)

    packet = {
        "generated_at": utcnow(),
        "patient_id": args.patient_id,
        "payer": patient["payer"],
        "cpt": args.cpt,
        "procedure": label,
        "required_documents": required,
        "present": present,
        "missing": missing,
        "ready_to_submit": not missing,
        "estimated_first_pass_likelihood": round(max(0.05, 1.0 - min(risk, 0.95)), 2),
        "draft_justification": _justification(label, patient, missing),
        "next_steps": (
            ["submit the packet and log the reference number"]
            if not missing
            else [_document_action(doc) for doc in missing]
        ),
    }
    return packet


DOC_ACTIONS = {
    "prior_auth": "request the authorisation number from the payer portal",
    "referral": "confirm the referring provider's order is on file",
    "imaging_report": "attach the radiology report from the last 90 days",
    "clinical_notes": "attach the most recent clinic note documenting failed conservative care",
    "conservative_tx_trial": "document 6+ weeks of conservative therapy (PT notes, medications)",
    "plan_of_care": "attach the signed therapy plan of care",
}


def _document_action(doc: str) -> str:
    return DOC_ACTIONS.get(doc, f"obtain {doc.replace('_', ' ')} from the care team")


def _justification(label: str, patient: dict[str, Any], missing: list[str]) -> str:
    lines = [
        f"Request for prior authorisation: {label}.",
        f"Patient {patient['id']} (MRN on file), payer {patient['payer']}.",
        "Clinical summary: symptoms persistent despite conservative management; "
        "procedure is medically necessary to establish diagnosis and guide treatment.",
        "Supporting documentation attached: " + (", ".join(patient.get("documents") or []) or "none on file") + ".",
    ]
    if missing:
        lines.append("Outstanding before submission: " + ", ".join(m.replace("_", " ") for m in missing) + ".")
    lines.append("Requesting expedited review within standard turnaround.")
    return "\n".join(lines)


def billing_tools() -> list[ToolSpec]:
    return [
        ToolSpec(
            name="claim_scrub",
            description=(
                "Pre-submission claim review. Checks CPT/ICD compatibility, authorisation "
                "requirements and amount thresholds, then returns a 0-1 denial risk with the "
                "specific fix for every finding."
            ),
            args_model=ClaimArgs,
            handler=claim_scrub,
            tags=("revenue", "read"),
        ),
        ToolSpec(
            name="billing_summary",
            description=(
                "Revenue-cycle exposure: charges and risk-adjusted dollars by payer, plus the "
                "highest-value claims to fix first."
            ),
            args_model=SummaryArgs,
            handler=billing_summary,
            tags=("revenue", "analytics"),
        ),
        ToolSpec(
            name="build_prior_auth_packet",
            description=(
                "Assembles a prior-authorisation packet: required documents for the procedure, "
                "what is already on file, what is missing, a draft justification letter and an "
                "estimated first-pass approval likelihood."
            ),
            args_model=PriorAuthArgs,
            handler=build_prior_auth_packet,
            tags=("revenue", "phi"),
        ),
    ]
