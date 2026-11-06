"""Patient lookup.

Every response passes through the registry's redactor, so a caller without the
``phi`` permission and a purpose of use sees masked fields - the handler does not
have to remember to mask anything.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ..models import utcnow
from .registry import ToolContext, ToolSpec


class PatientGetArgs(BaseModel):
    patient_id: str = Field(..., description="Patient identifier, e.g. P0007.")
    include_clinical: bool = Field(False, description="Include allergies and document checklist.")


def patient_get(ctx: ToolContext, args: PatientGetArgs) -> dict[str, Any]:
    patient = ctx.store.get_patient(args.patient_id)
    if patient is None:
        raise KeyError(f"patient {args.patient_id} not found")

    record: dict[str, Any] = {
        "id": patient["id"],
        "mrn": patient["mrn"],
        "name": patient["name"],
        "dob": patient["dob"],
        "phone": patient["phone"],
        "email": patient["email"],
        "address": patient["address"],
        "payer": patient["payer"],
        "risk_score": patient["risk_score"],
        "primary_care": patient["primary_care"],
        "retrieved_at": utcnow(),
    }
    if args.include_clinical:
        record["allergies"] = patient["allergies"]
        record["documents"] = patient["documents"]

    bed = next((b for b in ctx.store.beds() if b["patient_id"] == patient["id"]), None)
    record["current_bed"] = bed["id"] if bed else None
    record["current_ward"] = bed["ward"] if bed else None
    return record


class PatientSearchArgs(BaseModel):
    query: str = Field(..., min_length=1, max_length=80, description="Patient id, MRN, or partial name.")
    limit: int = Field(10, ge=1, le=50)


def patient_search(ctx: ToolContext, args: PatientSearchArgs) -> dict[str, Any]:
    matches = ctx.store.search_patients(args.query, limit=args.limit)
    return {
        "query": args.query,
        "count": len(matches),
        "results": [
            {"id": p["id"], "mrn": p["mrn"], "name": p["name"], "payer": p["payer"]}
            for p in matches
        ],
        "note": "names are masked unless the caller holds the phi permission and states a purpose of use",
    }


def patient_tools() -> list[ToolSpec]:
    return [
        ToolSpec(
            name="patient_get",
            description=(
                "Retrieves one patient record with demographics, coverage, risk score and current "
                "bed. Returns masked identifiers unless the caller may see PHI."
            ),
            args_model=PatientGetArgs,
            handler=patient_get,
            tags=("patient", "phi"),
        ),
        ToolSpec(
            name="patient_search",
            description="Finds patients by id, MRN or partial name.",
            args_model=PatientSearchArgs,
            handler=patient_search,
            tags=("patient", "phi"),
        ),
    ]
