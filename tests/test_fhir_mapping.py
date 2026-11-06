from __future__ import annotations

import pytest

from hospital_agent.fhir import mapping


def test_patient_uses_mrn_identifier_as_id():
    mapped = mapping.patient_row(
        {
            "resourceType": "Patient",
            "id": "fhir-1",
            "identifier": [{"type": {"coding": [{"code": "MR"}]}, "value": "200001"}],
            "name": [{"given": ["Ana"], "family": "Silva"}],
            "birthDate": "1979-03-14",
        }
    )
    assert mapped.row["id"] == "200001"
    assert mapped.row["mrn"] == "200001"
    assert mapped.row["name"] == "Ana Silva"
    assert mapped.warnings == []


def test_patient_prefers_configured_mrn_system():
    resource = {
        "resourceType": "Patient",
        "id": "fhir-1",
        "identifier": [
            {"system": "http://other.example/mrn", "type": {"coding": [{"code": "MR"}]}, "value": "111"},
            {"system": "http://hospital.example.org/mrn", "value": "222"},
        ],
        "name": [{"text": "Ana Silva"}],
        "birthDate": "1979-03-14",
    }
    mapped = mapping.patient_row(resource, mrn_system="http://hospital.example.org/mrn")
    assert mapped.row["id"] == "222"


def test_patient_without_identifier_falls_back_to_logical_id_and_warns():
    mapped = mapping.patient_row(
        {"resourceType": "Patient", "id": "fhir-9", "name": [{"family": "Diaz"}], "birthDate": "1990-07-02"}
    )
    assert mapped.row["id"] == "fhir-9"
    assert mapped.row["mrn"] == ""
    assert any("logical id" in w for w in mapped.warnings)


def test_patient_without_birth_date_is_refused_not_defaulted():
    with pytest.raises(mapping.MappingError, match="birthDate"):
        mapping.patient_row({"resourceType": "Patient", "id": "fhir-2", "name": [{"text": "Bo Chen"}]})


def test_patient_without_name_is_refused():
    with pytest.raises(mapping.MappingError, match="name"):
        mapping.patient_row({"resourceType": "Patient", "id": "fhir-3", "birthDate": "1990-01-01"})


def test_patient_contact_and_address_are_mapped():
    mapped = mapping.patient_row(
        {
            "resourceType": "Patient",
            "id": "fhir-1",
            "name": [{"use": "official", "given": ["Ana", "Maria"], "family": "Silva"}],
            "birthDate": "1979-03-14",
            "telecom": [{"system": "phone", "value": "555-0201"}, {"system": "email", "value": "a@example.org"}],
            "address": [{"line": ["12 Oak Street"], "city": "Testville", "state": "TS", "postalCode": "00501"}],
        }
    )
    assert mapped.row["phone"] == "555-0201"
    assert mapped.row["email"] == "a@example.org"
    assert mapped.row["address"] == "12 Oak Street, Testville, TS, 00501"
    assert mapped.row["name"] == "Ana Maria Silva"


def test_coverage_returns_patient_and_payer():
    patient_id, payer = mapping.coverage_payer(
        {
            "resourceType": "Coverage",
            "id": "cov-1",
            "beneficiary": {"reference": "Patient/fhir-pat-1"},
            "payor": [{"display": "BlueShield"}],
        }
    )
    assert patient_id == "fhir-pat-1"
    assert payer == "BlueShield"


def test_coverage_without_beneficiary_is_refused():
    with pytest.raises(mapping.MappingError):
        mapping.coverage_payer({"resourceType": "Coverage", "id": "cov-2", "payor": [{"display": "Aetna"}]})


@pytest.mark.parametrize(
    ("fhir_status", "expected"),
    [("booked", "scheduled"), ("pending", "scheduled"), ("fulfilled", "completed"), ("cancelled", "cancelled"), ("noshow", "no_show")],
)
def test_appointment_status_is_translated(fhir_status, expected):
    mapped = mapping.appointment_row(
        {
            "resourceType": "Appointment",
            "id": "appt-1",
            "status": fhir_status,
            "start": "2026-12-01T09:00:00Z",
            "participant": [{"actor": {"reference": "Patient/fhir-1"}}],
        },
        patient_id="200001",
    )
    assert mapped.row["status"] == expected


def test_appointment_in_error_is_not_ingested():
    with pytest.raises(mapping.MappingError, match="status"):
        mapping.appointment_row(
            {
                "resourceType": "Appointment",
                "id": "appt-3",
                "status": "entered-in-error",
                "start": "2026-12-01T09:00:00Z",
                "participant": [{"actor": {"reference": "Patient/fhir-1"}}],
            },
            patient_id="200001",
        )


def test_appointment_maps_department_provider_and_lead_time():
    mapped = mapping.appointment_row(
        {
            "resourceType": "Appointment",
            "id": "appt-1",
            "status": "booked",
            "start": "2026-12-01T09:00:00Z",
            "created": "2026-11-20T15:30:00Z",
            "serviceType": {"text": "Orthopedics"},
            "appointmentType": {"text": "Follow-up"},
            "participant": [
                {"actor": {"reference": "Patient/fhir-1"}},
                {"actor": {"reference": "Practitioner/pr-1", "display": "Dr. Example 2"}},
            ],
        },
        patient_id="200001",
    )
    assert mapped.row["dept"] == "Orthopedics"
    assert mapped.row["provider"] == "Dr. Example 2"
    assert mapped.row["appt_type"] == "follow_up"
    assert mapped.row["start_ts"] == "2026-12-01T09:00:00+00:00"
    assert mapped.row["lead_days"] == 11
    assert any("distance" in w for w in mapped.warnings)


def test_appointment_without_practitioner_keeps_provider_unassigned():
    mapped = mapping.appointment_row(
        {
            "resourceType": "Appointment",
            "id": "appt-2",
            "status": "booked",
            "start": "2026-12-02T14:00:00+00:00",
            "participant": [{"actor": {"reference": "Patient/fhir-1"}}],
        },
        patient_id="200001",
    )
    assert mapped.row["provider"] == "Unassigned"
    assert mapped.row["dept"] == "General"


def test_claim_amount_is_cents_without_float_drift():
    mapped = mapping.claim_row(
        {
            "resourceType": "Claim",
            "id": "claim-1",
            "status": "active",
            "total": {"value": "3250.00", "currency": "USD"},
            "insurer": {"display": "BlueShield"},
            "item": [{"productOrService": {"coding": [{"code": "27447"}]}, "preAuthRef": "AUTH-7781"}],
            "diagnosis": [{"diagnosisCodeableConcept": {"coding": [{"code": "M17.11"}]}}],
        },
        patient_id="200001",
    )
    assert mapped.row["amount_cents"] == 325_000
    assert mapped.row["cpt"] == "27447"
    assert mapped.row["icd10"] == "M17.11"
    assert mapped.row["auth_on_file"] == 1
    assert mapped.row["status"] == "submitted"


def test_claim_without_preauth_is_not_authorised():
    mapped = mapping.claim_row(
        {
            "resourceType": "Claim",
            "id": "claim-2",
            "status": "active",
            "item": [{"productOrService": {"text": "99213"}, "net": {"value": "125.00"}}],
        },
        patient_id="fhir-pat-3",
        payer_fallback="SelfPay",
    )
    assert mapped.row["auth_on_file"] == 0
    assert mapped.row["amount_cents"] == 12_500
    assert mapped.row["payer"] == "SelfPay"
    assert mapped.row["submitted_at"] is None


def test_claim_without_line_item_is_refused():
    with pytest.raises(mapping.MappingError, match="line item"):
        mapping.claim_row(
            {"resourceType": "Claim", "id": "claim-3", "status": "active", "total": {"value": "1.00"}},
            patient_id="200001",
        )


def test_timestamp_normalisation():
    assert mapping.normalize_ts("2026-12-01T09:00:00Z") == "2026-12-01T09:00:00+00:00"
    assert mapping.normalize_ts("2026-12-01T11:00:00+02:00") == "2026-12-01T09:00:00+00:00"
    with pytest.raises(mapping.MappingError):
        mapping.normalize_ts("next tuesday")


def test_cents_rounding():
    assert mapping.to_cents("19.99") == 1999
    assert mapping.to_cents(0.1) == 10
    assert mapping.to_cents("3250.005") == 325_001
