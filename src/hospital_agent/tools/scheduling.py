"""Outpatient scheduling with an explainable no-show model.

The differentiating idea: a slot is not equally valuable to every patient. We
score `expected attendance x time-to-care` and offer the slots that maximise
completed visits, not just the earliest free hour.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..fhir.writeback import mirror_appointment
from ..models import utcnow
from .registry import ToolContext, ToolSpec
from .scoring import no_show_probability

WORK_HOURS = list(range(9, 17))


class RiskArgs(BaseModel):
    patient_id: str | None = Field(None, description="Patient to assess. Uses their next scheduled appointment when present.")
    lead_days: int | None = Field(None, ge=0, le=365, description="Days between booking and visit.")
    prior_no_shows: int | None = Field(None, ge=0, le=20)
    appt_type: Literal["new", "follow_up"] = "new"
    distance_km: float | None = Field(None, ge=0, le=500)
    payer: str | None = None


def appointment_risk(ctx: ToolContext, args: RiskArgs) -> dict[str, Any]:
    features: dict[str, Any] = {
        "lead_days": args.lead_days or 7,
        "prior_no_shows": args.prior_no_shows or 0,
        "appt_type": args.appt_type,
        "payer": args.payer or "Unknown",
        "distance_km": args.distance_km if args.distance_km is not None else 10.0,
        "has_contact": True,
    }

    source = "provided"
    if args.patient_id:
        patient = ctx.store.get_patient(args.patient_id)
        if patient is None:
            raise KeyError(f"patient {args.patient_id} not found")
        upcoming = [
            a for a in ctx.store.appointments() if a["patient_id"] == args.patient_id
        ]
        if upcoming:
            nxt = upcoming[0]
            features.update(
                {
                    "lead_days": args.lead_days if args.lead_days is not None else nxt["lead_days"],
                    "prior_no_shows": args.prior_no_shows if args.prior_no_shows is not None else nxt["prior_no_shows"],
                    "appt_type": args.appt_type if args.appt_type != "new" else nxt["appt_type"],
                    "payer": args.payer or patient["payer"],
                    "distance_km": args.distance_km if args.distance_km is not None else nxt["distance_km"],
                }
            )
            source = "appointment"
        else:
            features["payer"] = args.payer or patient["payer"]
            source = "patient"

    result = no_show_probability(**features)
    return {
        "patient_id": args.patient_id,
        "features": features,
        "features_source": source,
        **result,
        "overbook_recommended": result["probability"] >= 0.35,
    }


class SlotsArgs(BaseModel):
    dept: str = Field(..., description="Department, e.g. Cardiology, Orthopedics, Imaging.")
    provider: str | None = Field(None, description="Provider name; omit to search all providers in the department.")
    days: int = Field(7, ge=1, le=30)
    limit: int = Field(5, ge=1, le=20)
    patient_id: str | None = Field(None, description="Rank slots for this patient's attendance likelihood.")


def find_appointment_slots(ctx: ToolContext, args: SlotsArgs) -> dict[str, Any]:
    providers = [args.provider] if args.provider else _providers_for(ctx, args.dept)
    if not providers:
        raise ValueError(f"no providers found for department '{args.dept}'")

    patient_risk = None
    if args.patient_id:
        patient_risk = appointment_risk(
            ctx, RiskArgs(patient_id=args.patient_id, appt_type="new")
        )["probability"]

    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    booked = {
        (a["provider"], a["start_ts"][:13])
        for a in ctx.store.appointments(status="scheduled")
    }

    candidates: list[dict[str, Any]] = []
    for day_offset in range(args.days):
        day = (now + timedelta(days=day_offset + 1)).replace(hour=0)
        if day.weekday() >= 5:
            continue
        for provider in providers:
            for hour in WORK_HOURS:
                slot = day.replace(hour=hour)
                if (provider, slot.isoformat(timespec="seconds")[:13]) in booked:
                    continue
                if _blocked(provider, slot):
                    continue
                lead_days = (slot.date() - now.date()).days
                risk = patient_risk if patient_risk is not None else 0.25
                attendance = 1.0 - risk
                urgency = 1.0 / (1.0 + 0.18 * lead_days)
                value = round(attendance * urgency, 3)
                candidates.append(
                    {
                        "provider": provider,
                        "start_ts": slot.isoformat(timespec="seconds"),
                        "lead_days": lead_days,
                        "expected_attendance": round(attendance, 2),
                        "slot_value": value,
                        "why": "earlier slot, higher expected attendance" if lead_days <= 3 else "later slot, more cancellation risk",
                    }
                )

    candidates.sort(key=lambda c: (-c["slot_value"], c["start_ts"]))
    return {
        "dept": args.dept,
        "generated_at": utcnow(),
        "slots": candidates[: args.limit],
        "total_considered": len(candidates),
        "ranking": "expected_attendance x time_to_care",
    }


def _providers_for(ctx: ToolContext, dept: str) -> list[str]:
    return sorted({
        a["provider"] for a in ctx.store.appointments(status=None) if a["dept"].lower() == dept.lower()
    })


def _blocked(provider: str, slot: datetime) -> bool:
    """Deterministic stand-in for a real provider calendar."""
    seed = f"{provider}|{slot.isoformat()}".encode()
    return hashlib.sha256(seed).digest()[0] % 9 == 0


class ScheduleArgs(BaseModel):
    patient_id: str = Field(..., description="Patient to book.")
    dept: str = Field(..., description="Department.")
    provider: str = Field(..., description="Provider name.")
    start_ts: str = Field(..., description="ISO-8601 start time.")
    appt_type: Literal["new", "follow_up"] = "new"


def schedule_appointment(ctx: ToolContext, args: ScheduleArgs) -> dict[str, Any]:
    patient = ctx.store.get_patient(args.patient_id)
    if patient is None:
        raise KeyError(f"patient {args.patient_id} not found")

    start = datetime.fromisoformat(args.start_ts.replace("Z", "+00:00"))
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if start <= datetime.now(timezone.utc):
        raise ValueError("appointment must be in the future")

    existing = ctx.store.appointments(provider=args.provider, status="scheduled")
    if any(a["start_ts"][:16] == args.start_ts[:16] for a in existing):
        raise ValueError(f"{args.provider} is already booked at {args.start_ts}")

    lead_days = max(0, (start.date() - datetime.now(timezone.utc).date()).days)
    # Deterministic, not ``hash()``: the id must survive a process restart so a
    # retried approval cannot create a second appointment (or a second FHIR one).
    fingerprint = hashlib.sha256(f"{args.patient_id}|{start.isoformat(timespec='seconds')}".encode()).hexdigest()
    payload = {
        "id": f"A{fingerprint[:8]}",
        "patient_id": args.patient_id,
        "dept": args.dept,
        "provider": args.provider,
        "start_ts": start.isoformat(timespec="seconds"),
        "appt_type": args.appt_type,
        "lead_days": lead_days,
        "prior_no_shows": 0,
        "distance_km": 10.0,
    }

    if ctx.store.get_appointment(payload["id"]) is not None:
        raise ValueError(f"appointment {payload['id']} already exists for {args.patient_id}")

    # When an EHR is connected this writes the FHIR Appointment first: if the
    # server refuses, nothing is booked locally and the approval can be retried.
    mirrored = mirror_appointment(ctx.settings, appointment=payload, patient=patient, store=ctx.store)

    appt = ctx.store.create_appointment(payload)
    if mirrored.written:
        ctx.store.link_fhir(mirrored.source, "Appointment", str(mirrored.external_id), appt["id"])
    risk = no_show_probability(
        lead_days=lead_days,
        prior_no_shows=0,
        appt_type=args.appt_type,
        payer=patient["payer"],
        distance_km=10.0,
    )
    result: dict[str, Any] = {
        "appointment": appt,
        "no_show_risk": risk,
        "reminders": ["72h confirmation", "24h reminder"] if risk["probability"] >= 0.35 else ["24h reminder"],
    }
    if mirrored.written:
        result["external"] = {"system": "fhir", "source": mirrored.source, "id": mirrored.external_id}
    if mirrored.warnings:
        result["warnings"] = mirrored.warnings
    return result


def scheduling_tools() -> list[ToolSpec]:
    return [
        ToolSpec(
            name="appointment_risk",
            description=(
                "Predicts the probability a patient misses an appointment, explains the drivers "
                "(lead time, prior no-shows, payer, distance, visit type) and returns a concrete "
                "mitigation plus an overbooking recommendation."
            ),
            args_model=RiskArgs,
            handler=appointment_risk,
            tags=("access", "read"),
        ),
        ToolSpec(
            name="find_appointment_slots",
            description=(
                "Finds open slots for a department or provider and ranks them by expected "
                "attendance x time-to-care, optionally personalised to a patient's no-show risk."
            ),
            args_model=SlotsArgs,
            handler=find_appointment_slots,
            tags=("access", "read"),
        ),
        ToolSpec(
            name="schedule_appointment",
            description=(
                "Books an appointment after checking the patient exists, the slot is free and in "
                "the future. Requires human approval because it mutates the schedule."
            ),
            args_model=ScheduleArgs,
            handler=schedule_appointment,
            mutates=True,
            tags=("access", "write"),
        ),
    ]
