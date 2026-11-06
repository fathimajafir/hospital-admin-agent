"""Patient flow: capacity, discharge readiness, staffing gaps.

These three answer the questions a hospital operations lead actually asks at
07:00: where is the pressure, who can go home, and who is short-staffed.
"""

from __future__ import annotations

import math
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..models import utcnow
from .registry import ToolContext, ToolSpec
from .scoring import capacity_pressure, discharge_readiness

OCCUPIED = {"occupied", "boarding"}
WARDS = ["ED", "ICU", "MED-A", "MED-B", "SURG", "PEDS"]


class CapacityArgs(BaseModel):
    ward: str | None = Field(None, description="Ward code (ED, ICU, MED-A, MED-B, SURG, PEDS). Omit for all.")
    include_recommendations: bool = Field(True, description="Include suggested next actions per ward.")


def capacity_snapshot(ctx: ToolContext, args: CapacityArgs) -> dict[str, Any]:
    beds = ctx.store.beds(args.ward)
    if not beds:
        raise ValueError(f"ward '{args.ward}' not found")

    wards: dict[str, list[dict[str, Any]]] = {}
    for bed in beds:
        wards.setdefault(bed["ward"], []).append(bed)

    rows: list[dict[str, Any]] = []
    total_beds = total_occupied = total_boarders = 0
    worst_pressure = 0.0

    for ward, ward_beds in wards.items():
        total = len(ward_beds)
        occupied = sum(1 for b in ward_beds if b["status"] in OCCUPIED)
        boarders = sum(1 for b in ward_beds if b["status"] == "boarding")
        free = sum(1 for b in ward_beds if b["status"] == "free")
        blocked = sum(1 for b in ward_beds if b["status"] in {"blocked", "cleaning"})
        occupancy = round(occupied / total, 2) if total else 0.0

        # Proxy for ED dwell time: boarding hours dominate. Replace with the EHR
        # timestamp feed when wiring this to a real system.
        ed_wait_hours = round(1.5 + boarders * 0.8 + max(0.0, occupancy - 0.85) * 20, 1) if ward == "ED" else 0.0
        pressure = capacity_pressure(occupancy=occupancy, boarders=boarders, ed_wait_hours=ed_wait_hours)

        total_beds += total
        total_occupied += occupied
        total_boarders += boarders
        worst_pressure = max(worst_pressure, pressure["pressure"])

        row: dict[str, Any] = {
            "ward": ward,
            "beds": total,
            "occupied": occupied,
            "free": free,
            "unavailable": blocked,
            "boarders": boarders,
            "occupancy": occupancy,
            "pressure": pressure["pressure"],
            "band": pressure["band"],
        }
        if ward == "ED":
            row["ed_wait_hours"] = ed_wait_hours
        if args.include_recommendations:
            row["action"] = _capacity_action(row)
        rows.append(row)

    rows.sort(key=lambda r: -r["pressure"])
    overall = capacity_pressure(
        occupancy=total_occupied / total_beds if total_beds else 0.0,
        boarders=total_boarders,
        ed_wait_hours=max((r.get("ed_wait_hours", 0.0) for r in rows), default=0.0),
    )

    return {
        "generated_at": utcnow(),
        "totals": {
            "beds": total_beds,
            "occupied": total_occupied,
            "occupancy": round(total_occupied / total_beds, 2) if total_beds else 0.0,
            "boarders": total_boarders,
        },
        "pressure": overall,
        "wards": rows,
        "escalation_required": worst_pressure >= 0.85,
    }


def _capacity_action(row: dict[str, Any]) -> str:
    if row["band"] == "critical":
        return "activate surge plan: escalate to the house supervisor and open discharge lounge capacity now"
    if row["boarders"] >= 3:
        return "pull boarders into available med-surg beds; assign a flow nurse to ED"
    if row["occupancy"] >= 0.9:
        return "review tomorrow's elective list and accelerate discharges this afternoon"
    if row["free"] >= 3:
        return "ward has capacity; accept transfers or pull from ED"
    return "no action needed"


class DischargeArgs(BaseModel):
    ward: str | None = Field(None, description="Restrict to one ward.")
    min_readiness: float = Field(0.0, ge=0.0, le=1.0, description="Only show plans at or above this readiness.")
    limit: int = Field(20, ge=1, le=100)


def discharge_board(ctx: ToolContext, args: DischargeArgs) -> dict[str, Any]:
    plans = ctx.store.discharges(args.ward)
    today = date.today()
    items: list[dict[str, Any]] = []

    for plan in plans:
        assessment = discharge_readiness(plan["barriers"])
        if assessment["readiness"] < args.min_readiness:
            continue
        target = date.fromisoformat(plan["target_date"]) if plan["target_date"] else today
        items.append(
            {
                "patient_id": plan["patient_id"],
                "ward": plan["ward"],
                "target_date": plan["target_date"],
                "days_until_target": (target - today).days,
                "readiness": assessment["readiness"],
                "band": assessment["band"],
                "blocking": assessment["blocking"],
                "next_actions": assessment["next_actions"],
                "at_risk": (target - today).days <= 0 and assessment["readiness"] < 0.85,
            }
        )

    items.sort(key=lambda i: (i["readiness"], -i["days_until_target"]))
    dischargeable_today = sum(1 for i in items if i["readiness"] >= 0.85 and i["days_until_target"] <= 0)

    return {
        "generated_at": utcnow(),
        "count": len(items),
        "ready_today": dischargeable_today,
        "at_risk": sum(1 for i in items if i["at_risk"]),
        "plans": items[: args.limit],
        "note": "readiness is a deterministic barrier score; clinical discharge decisions remain with the care team",
    }


class BedAssignArgs(BaseModel):
    patient_id: str = Field(..., description="Patient to place.")
    bed_id: str = Field(..., description="Target bed, e.g. A03. Must be free or being cleaned.")


def bed_assign(ctx: ToolContext, args: BedAssignArgs) -> dict[str, Any]:
    patient = ctx.store.get_patient(args.patient_id)
    if patient is None:
        raise KeyError(f"patient {args.patient_id} not found")
    bed = ctx.store.get_bed(args.bed_id)
    if bed is None:
        raise KeyError(f"bed {args.bed_id} not found")
    previous = next((b for b in ctx.store.beds() if b["patient_id"] == args.patient_id), None)
    updated = ctx.store.assign_bed(args.patient_id, args.bed_id)
    return {
        "patient_id": args.patient_id,
        "bed": updated,
        "previous_bed": previous["id"] if previous else None,
        "note": "clinical appropriateness of the placement remains the charge nurse's decision",
    }


class StaffingArgs(BaseModel):
    ward: str | None = Field(None, description="Ward code. Omit for all wards.")
    shift: Literal["day", "night"] | None = Field(None, description="Shift to evaluate. Omit for both.")


#: ward -> (acuity multiplier, patients per RN). Mirrors the seed configuration.
STAFFING_MODEL: dict[str, tuple[float, float]] = {
    "ED": (1.20, 3.0),
    "ICU": (1.50, 1.5),
    "MED-A": (1.00, 4.5),
    "MED-B": (1.00, 4.5),
    "SURG": (1.30, 2.5),
    "PEDS": (1.10, 3.0),
}


def staffing_gaps(ctx: ToolContext, args: StaffingArgs) -> dict[str, Any]:
    beds = ctx.store.beds(args.ward)
    shifts = ctx.store.shifts(args.ward)
    if not beds:
        raise ValueError(f"ward '{args.ward}' not found")

    census: dict[str, int] = {}
    for bed in beds:
        if bed["status"] in OCCUPIED:
            census[bed["ward"]] = census.get(bed["ward"], 0) + 1

    assigned: dict[tuple[str, str, str], int] = {
        (s["ward"], s["shift"], s["role"]): s["assigned"] for s in shifts
    }

    rows: list[dict[str, Any]] = []
    for ward, patients in sorted(census.items()):
        acuity, ratio = STAFFING_MODEL.get(ward, (1.0, 4.5))
        required_rn = max(1, math.ceil(patients * acuity / ratio))
        required_md = max(1, math.ceil(patients / 12))
        for shift in ("day", "night"):
            if args.shift and shift != args.shift:
                continue
            have_rn = assigned.get((ward, shift, "RN"), 0)
            have_md = assigned.get((ward, shift, "MD"), 0)
            gap_rn = int(required_rn - have_rn)
            gap_md = int(required_md - have_md)
            gap_total = max(gap_rn, 0) + max(gap_md, 0)
            rows.append(
                {
                    "ward": ward,
                    "shift": shift,
                    "census": patients,
                    "acuity": acuity,
                    "required_rn": required_rn,
                    "assigned_rn": have_rn,
                    "gap_rn": gap_rn,
                    "required_md": required_md,
                    "assigned_md": have_md,
                    "gap_md": gap_md,
                    "severity": "critical" if gap_total >= 2 else "moderate" if gap_total == 1 else "covered",
                    "action": _staffing_action(ward, shift, gap_rn, gap_md),
                }
            )

    rows.sort(key=lambda r: (-max(r["gap_rn"], 0), -max(r["gap_md"], 0)))
    return {
        "generated_at": utcnow(),
        "shifts": rows,
        "critical_shifts": sum(1 for r in rows if r["severity"] == "critical"),
        "note": "required staffing uses census x acuity / target ratio; confirm against your unit's actual staffing grid",
    }


def _staffing_action(ward: str, shift: str, gap_rn: int, gap_md: int) -> str:
    if gap_rn <= 0 and gap_md <= 0:
        return "covered"
    parts = []
    if gap_rn > 0:
        parts.append(f"call in {gap_rn} RN" + ("" if gap_rn == 1 else "s"))
    if gap_md > 0:
        parts.append(f"add {gap_md} physician" + ("" if gap_md == 1 else "s") + " coverage")
    return f"{shift} {ward}: " + " and ".join(parts) + "; consider closing beds if unfilled"


def capacity_tools() -> list[ToolSpec]:
    return [
        ToolSpec(
            name="capacity_snapshot",
            description=(
                "Real-time bed and patient-flow picture by ward: occupancy, boarders, ED wait, "
                "a 0-1 pressure score with band, and the next action for each ward."
            ),
            args_model=CapacityArgs,
            handler=capacity_snapshot,
            tags=("flow", "read"),
        ),
        ToolSpec(
            name="discharge_board",
            description=(
                "Discharge readiness board. Scores each planned discharge from its barriers "
                "(imaging, labs, PT, social work, transport, authorisation), names the blocking "
                "owner and the next action, and flags plans that will miss their target date."
            ),
            args_model=DischargeArgs,
            handler=discharge_board,
            tags=("flow", "read"),
        ),
        ToolSpec(
            name="bed_assign",
            description=(
                "Moves a patient into a specific bed after checking the bed is free. Requires "
                "clinical permission and human approval because it mutates bed state."
            ),
            args_model=BedAssignArgs,
            handler=bed_assign,
            mutates=True,
            tags=("flow", "write"),
        ),
        ToolSpec(
            name="staffing_gaps",
            description=(
                "Compares census x acuity against the assigned roster per ward and shift, and "
                "returns the RN and physician gap with a concrete fill action."
            ),
            args_model=StaffingArgs,
            handler=staffing_gaps,
            tags=("workforce", "read"),
        ),
    ]
