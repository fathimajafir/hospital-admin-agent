"""Outbound path: a booked appointment becomes a FHIR Appointment.

Deliberately one resource and one direction. This runs inside
``schedule_appointment``, which the policy engine already forces through human
approval, so nothing reaches the EHR without a recorded decision.

Ordering matters: the FHIR write happens *before* the local insert. If the
server rejects it, no local row exists and the approval stays executable, so the
operator can fix the cause and retry. The alternative - commit locally, then try
to mirror - leaves the schedule and the EHR disagreeing, which is the failure
mode that makes clinicians stop trusting the tool.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .client import build_client
from .mapping import normalize_ts


@dataclass
class WriteBack:
    external_id: str | None
    source: str
    warnings: list[str] = field(default_factory=list)

    @property
    def written(self) -> bool:
        return self.external_id is not None


def appointment_to_fhir(
    appointment: dict[str, Any],
    patient: dict[str, Any],
    *,
    patient_reference: str | None = None,
) -> dict[str, Any]:
    """Build the R4 Appointment. Pure: no client, no clock, easy to assert on.

    No ``end`` is set: the local schedule does not model visit duration, and
    guessing 30 minutes would be a fabrication the receiving system cannot tell
    apart from a real booking.
    """
    reference = patient_reference or str(patient.get("mrn") or patient["id"])

    return {
        "resourceType": "Appointment",
        "status": "booked",
        "start": normalize_ts(appointment["start_ts"]),
        "serviceType": {"text": str(appointment["dept"])},
        "description": f"{appointment['dept']} ({appointment.get('appt_type', 'new')})",
        "participant": [
            {
                "actor": {
                    "reference": f"Patient/{reference}",
                    "display": str(patient.get("name") or ""),
                },
                "status": "accepted",
            },
            {
                "actor": {"display": str(appointment["provider"])},
                "status": "accepted",
            },
        ],
    }


def mirror_appointment(
    settings: Any,
    *,
    appointment: dict[str, Any],
    patient: dict[str, Any],
    store: Any,
    transport: Any = None,
) -> WriteBack:
    """Create the remote Appointment when write-back is enabled.

    Raises ``FhirError`` if the server rejects the resource; the caller must not
    write the local row in that case.
    """
    if not getattr(settings, "fhir_write_enabled", False):
        return WriteBack(external_id=None, source="")

    client = build_client(settings, transport=transport)
    try:
        source = client.source_label()
        reference = store.external_for_internal(source, "Patient", str(appointment["patient_id"]))
        payload = appointment_to_fhir(appointment, patient, patient_reference=reference)
        created = client.create("Appointment", payload)
    finally:
        client.close()

    external_id = str(created.get("id") or "") or None
    warnings: list[str] = []
    if external_id is None:
        warnings.append("FHIR server accepted the Appointment but returned no id")
    return WriteBack(external_id=external_id, source=source, warnings=warnings)
