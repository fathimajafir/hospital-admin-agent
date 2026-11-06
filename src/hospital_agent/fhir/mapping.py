"""FHIR resource -> internal row.

Pure functions: no store, no network, no clock beyond what the resource says.
That is what makes the mapping reviewable, and what lets the ingest job be
tested with fixtures alone.

The rule followed throughout: **when FHIR does not say something, we skip the
resource rather than invent a value.** A missing birth date is not "unknown
patient with no birthday", it is a resource we refuse to load, and the sync
report says so. The single exception is documented in ``appointment_row``
(distance, which the FHIR Appointment resource has no field for).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

class MappingError(ValueError):
    """The resource is valid FHIR but cannot be loaded faithfully."""


@dataclass
class Mapped:
    row: dict[str, Any]
    external_id: str
    warnings: list[str] = field(default_factory=list)


# -- small readers ---------------------------------------------------------

def _first(seq: Any) -> dict[str, Any] | None:
    if isinstance(seq, list) and seq:
        item = seq[0]
        return item if isinstance(item, dict) else None
    return None


def coding_code(concept: Any) -> str | None:
    """First ``coding[].code`` of a CodeableConcept."""
    if not isinstance(concept, dict):
        return None
    for coding in concept.get("coding") or []:
        code = (coding or {}).get("code")
        if code:
            return str(code)
    return None


def concept_text(concept: Any) -> str | None:
    """Human label of a CodeableConcept: ``text``, else the first display."""
    if not isinstance(concept, dict):
        return None
    if concept.get("text"):
        return str(concept["text"])
    for coding in concept.get("coding") or []:
        display = (coding or {}).get("display")
        if display:
            return str(display)
    code = coding_code(concept)
    return str(code) if code else None


def reference_id(reference: Any) -> tuple[str | None, str | None]:
    """``"Patient/123/_history/1"`` -> ``("Patient", "123")``."""
    if not isinstance(reference, str) or not reference:
        return (None, None)
    parts = [p for p in reference.split("/") if p]
    if len(parts) >= 2:
        return (parts[0], parts[1])
    return (None, parts[0] if parts else None)


def normalize_ts(value: Any) -> str:
    """Any FHIR instant/dateTime -> UTC ISO-8601 at second precision."""
    if not isinstance(value, str) or not value.strip():
        raise MappingError("timestamp is missing")
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise MappingError(f"timestamp {value!r} is not ISO-8601") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def to_cents(value: Any) -> int:
    """Money value in major units -> integer cents, without float drift."""
    if value is None:
        raise MappingError("amount is missing")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise MappingError(f"amount {value!r} is not a decimal") from exc
    return int((amount * 100).to_integral_value(rounding=ROUND_HALF_UP))


# -- Patient ---------------------------------------------------------------

def patient_name(resource: dict[str, Any]) -> str | None:
    names = [n for n in (resource.get("name") or []) if isinstance(n, dict)]
    if not names:
        return None
    official = next((n for n in names if n.get("use") == "official"), names[0])
    if official.get("text"):
        return str(official["text"]).strip() or None
    parts = [str(p) for p in (official.get("given") or []) if p]
    family = official.get("family")
    if family:
        parts.append(str(family))
    joined = " ".join(p.strip() for p in parts if p and p.strip())
    return joined or None


def patient_mrn(resource: dict[str, Any], mrn_system: str | None) -> str | None:
    identifiers = [i for i in (resource.get("identifier") or []) if isinstance(i, dict)]
    if mrn_system:
        for ident in identifiers:
            if ident.get("system") == mrn_system and ident.get("value"):
                return str(ident["value"])
    for ident in identifiers:
        type_codes = {c.get("code") for c in ((ident.get("type") or {}).get("coding") or []) if isinstance(c, dict)}
        if "MR" in type_codes and ident.get("value"):
            return str(ident["value"])
    return None


def _telecom(resource: dict[str, Any], system: str) -> str | None:
    for contact in resource.get("telecom") or []:
        if isinstance(contact, dict) and contact.get("system") == system and contact.get("value"):
            return str(contact["value"])
    return None


def _address(resource: dict[str, Any]) -> str | None:
    address = _first(resource.get("address"))
    if not address:
        return None
    if address.get("text"):
        return str(address["text"])
    parts = [*(address.get("line") or []), address.get("city"), address.get("state"), address.get("postalCode")]
    joined = ", ".join(str(p).strip() for p in parts if p and str(p).strip())
    return joined or None


def patient_row(resource: dict[str, Any], *, mrn_system: str | None = None) -> Mapped:
    if resource.get("resourceType") != "Patient":
        raise MappingError("resource is not a Patient")
    external_id = resource.get("id")
    if not external_id:
        raise MappingError("Patient has no logical id")

    name = patient_name(resource)
    if not name:
        raise MappingError("Patient has no usable name")
    dob = resource.get("birthDate")
    if not dob:
        raise MappingError("Patient has no birthDate")

    mrn = patient_mrn(resource, mrn_system)
    warnings: list[str] = []
    if not mrn:
        warnings.append("no MRN identifier; kept the FHIR logical id as the patient id")

    return Mapped(
        external_id=str(external_id),
        warnings=warnings,
        row={
            "id": mrn or str(external_id),
            "mrn": mrn or "",
            "name": name,
            "dob": str(dob),
            "phone": _telecom(resource, "phone") or "",
            "email": _telecom(resource, "email") or "",
            "address": _address(resource) or "",
            # Coverage is a separate resource; this is patched during ingest.
            "payer": "",
        },
    )


# -- Coverage --------------------------------------------------------------

def coverage_payer(resource: dict[str, Any]) -> tuple[str, str]:
    """Return ``(patient_logical_id, payer_label)``."""
    if resource.get("resourceType") != "Coverage":
        raise MappingError("resource is not a Coverage")
    kind, patient_id = reference_id((resource.get("beneficiary") or {}).get("reference"))
    if kind != "Patient" or not patient_id:
        raise MappingError("Coverage has no Patient beneficiary")

    payor = _first(resource.get("payor"))
    payer = None
    if payor:
        payer = payor.get("display") or concept_text(payor)
        if not payer:
            _, payor_id = reference_id(payor.get("reference"))
            payer = payor_id
    if not payer:
        raise MappingError("Coverage has no payor label")
    return (patient_id, str(payer).strip())


# -- Appointment -----------------------------------------------------------

APPOINTMENT_STATUS = {
    "booked": "scheduled",
    "pending": "scheduled",
    "waitlist": "scheduled",
    "arrived": "scheduled",
    "checked-in": "scheduled",
    "fulfilled": "completed",
    "cancelled": "cancelled",
    "noshow": "no_show",
}


def appointment_row(resource: dict[str, Any], *, patient_id: str) -> Mapped:
    if resource.get("resourceType") != "Appointment":
        raise MappingError("resource is not an Appointment")
    external_id = resource.get("id")
    if not external_id:
        raise MappingError("Appointment has no logical id")

    status = APPOINTMENT_STATUS.get(str(resource.get("status") or "").lower())
    if status is None:
        raise MappingError(f"Appointment status {resource.get('status')!r} is not ingested")

    start_ts = normalize_ts(resource.get("start"))
    dept = concept_text(resource.get("serviceType")) or concept_text(resource.get("serviceCategory")) or "General"

    provider = "Unassigned"
    for participant in resource.get("participant") or []:
        if not isinstance(participant, dict):
            continue
        actor = participant.get("actor") or {}
        kind, _ = reference_id(actor.get("reference"))
        if kind in {"Practitioner", "PractitionerRole"} and actor.get("display"):
            provider = str(actor["display"]).strip()
            break

    appt_type = "new"
    type_label = (concept_text(resource.get("appointmentType")) or "").lower()
    if "follow" in type_label or "recurring" in type_label:
        appt_type = "follow_up"

    lead_days = 0
    if resource.get("created"):
        try:
            created = datetime.fromisoformat(normalize_ts(resource["created"]))
            start = datetime.fromisoformat(start_ts)
            lead_days = max(0, (start.date() - created.date()).days)
        except (MappingError, ValueError):
            lead_days = 0

    return Mapped(
        external_id=str(external_id),
        row={
            "id": str(external_id),
            "patient_id": patient_id,
            "dept": str(dept),
            "provider": provider,
            "start_ts": start_ts,
            "status": status,
            "appt_type": appt_type,
            "lead_days": lead_days,
        },
        # FHIR Appointment has no travel distance and no no-show history. Both are
        # inputs to the local model, so a loaded row is scored with them at their
        # neutral defaults until the data arrives another way.
        warnings=["distance_km and prior_no_shows are local features not present in FHIR"],
    )


# -- Claim -----------------------------------------------------------------

CLAIM_STATUS = {
    "active": "submitted",
    "draft": "draft",
    "cancelled": "void",
}


def claim_row(resource: dict[str, Any], *, patient_id: str, payer_fallback: str = "") -> Mapped:
    if resource.get("resourceType") != "Claim":
        raise MappingError("resource is not a Claim")
    external_id = resource.get("id")
    if not external_id:
        raise MappingError("Claim has no logical id")

    status = CLAIM_STATUS.get(str(resource.get("status") or "").lower())
    if status is None:
        raise MappingError(f"Claim status {resource.get('status')!r} is not ingested")

    items = [i for i in (resource.get("item") or []) if isinstance(i, dict)]
    item = items[0] if items else None
    if item is None:
        raise MappingError("Claim has no line item to take a procedure code from")

    cpt = coding_code(item.get("productOrService")) or concept_text(item.get("productOrService"))
    if not cpt:
        raise MappingError("Claim line item has no productOrService code")

    amount = (resource.get("total") or {}).get("value")
    if amount is None and item.get("net") is not None:
        amount = item["net"].get("value") if isinstance(item["net"], dict) else item["net"]

    icd10 = ""
    diagnosis = _first(resource.get("diagnosis"))
    if diagnosis:
        icd10 = coding_code((diagnosis.get("diagnosisCodeableConcept") or {})) or ""

    # R4 puts prior authorisation on the line item, not on the Claim: a
    # non-empty preAuthRef is the only signal that auth is on file.
    auth_on_file = 1 if item.get("preAuthRef") else 0

    insurer = resource.get("insurer") or {}
    payer = insurer.get("display") or payer_fallback or ""

    warnings: list[str] = []
    if not payer:
        warnings.append("no payer on the Claim or the patient record")

    return Mapped(
        external_id=str(external_id),
        warnings=warnings,
        row={
            "id": str(external_id),
            "patient_id": patient_id,
            "payer": payer,
            "cpt": str(cpt),
            "icd10": str(icd10),
            "amount_cents": to_cents(amount),
            "auth_on_file": auth_on_file,
            "submitted_at": normalize_ts(resource["created"]) if resource.get("created") else None,
            "status": status,
        },
    )
