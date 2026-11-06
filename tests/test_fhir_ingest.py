from __future__ import annotations

import json
from pathlib import Path

import pytest

from hospital_agent.fhir import FhirIngest

FIXTURE = Path(__file__).parent / "fixtures" / "fhir" / "sample_bundle.json"


@pytest.fixture()
def bundle() -> dict:
    return json.loads(FIXTURE.read_text())


@pytest.fixture()
def ingest(container) -> FhirIngest:
    return FhirIngest(container.store, container.settings, audit=container.audit)


def test_bundle_ingest_counts_and_values(ingest, container, bundle):
    report = ingest.apply_bundle(bundle)

    assert report.created["Patient"] == 2
    assert report.skipped["Patient"] == 1  # missing birthDate
    assert report.updated["Coverage"] == 1
    assert report.skipped["Coverage"] == 1  # beneficiary not in the store
    assert report.created["Appointment"] == 2
    assert report.skipped["Appointment"] == 1  # entered-in-error
    assert report.created["Claim"] == 2
    assert report.skipped["Claim"] == 1  # patient not in the store

    patient = container.store.get_patient("200001")
    assert patient is not None
    assert patient["name"] == "Ana Maria Silva"
    assert patient["payer"] == "BlueShield"
    assert patient["phone"] == "555-0201"

    appointment = container.store.get_appointment("appt-1")
    assert appointment["patient_id"] == "200001"
    assert appointment["dept"] == "Orthopedics"
    assert appointment["status"] == "scheduled"
    assert appointment["appt_type"] == "follow_up"

    claim = container.store.get_claim("claim-1")
    assert claim["amount_cents"] == 325_000
    assert claim["auth_on_file"] == 1


def test_ingest_is_idempotent(ingest, container, bundle):
    ingest.apply_bundle(bundle)
    patients_after_first = len(container.store.search_patients("", limit=500))

    second = ingest.apply_bundle(bundle)

    assert second.totals["created"] == 0
    assert second.created.get("Patient", 0) == 0
    assert second.updated["Patient"] == 2
    assert len(container.store.search_patients("", limit=500)) == patients_after_first


def test_resync_preserves_local_only_fields(ingest, container, bundle):
    container.store._exec(
        "UPDATE patients SET risk_score=0.77, documents=? WHERE id=?",
        (json.dumps(["referral"]), "P0001"),
    )
    container.store.create_appointment(
        {
            "id": "appt-1",
            "patient_id": "P0001",
            "dept": "Stale",
            "provider": "Dr. Stale",
            "start_ts": "2026-12-01T09:00:00+00:00",
            "appt_type": "new",
            "lead_days": 3,
            "prior_no_shows": 4,
            "distance_km": 42.0,
        }
    )
    ingest.apply_bundle(bundle)
    ingest.apply_bundle(bundle)

    patient = container.store.get_patient("P0001")
    assert patient["risk_score"] == 0.77
    assert patient["documents"] == ["referral"]

    appointment = container.store.get_appointment("appt-1")
    assert appointment["distance_km"] == 42.0
    assert appointment["prior_no_shows"] == 4
    # Sourced fields did change.
    assert appointment["dept"] == "Orthopedics"
    assert appointment["patient_id"] == "200001"


def test_links_are_recorded_for_every_ingested_resource(ingest, container, bundle):
    ingest.apply_bundle(bundle, source="test")

    assert container.store.internal_for_fhir("test", "Patient", "fhir-pat-1") == "200001"
    assert container.store.internal_for_fhir("test", "Patient", "fhir-pat-3") == "fhir-pat-3"
    assert container.store.internal_for_fhir("test", "Coverage", "cov-1") == "200001"
    assert container.store.external_for_internal("test", "Patient", "200001") == "fhir-pat-1"
    assert len(container.store.fhir_links(limit=100)) == 7


def test_dry_run_writes_nothing(ingest, container, bundle):
    before = len(container.store.search_patients("", limit=500))

    report = ingest.apply_bundle(bundle, dry_run=True)

    assert report.dry_run is True
    assert report.created["Patient"] == 2
    assert len(container.store.search_patients("", limit=500)) == before
    assert container.store.get_patient("200001") is None
    assert container.store.fhir_links(limit=100) == []


def test_sync_records_an_audit_entry(ingest, container, bundle):
    ingest.apply_bundle(bundle)

    ok, detail = container.audit.verify()
    assert ok, detail
    syncs = [row for row in container.store.list_audit(limit=50) if row["action"] == "fhir.sync"]
    assert len(syncs) == 1
    assert syncs[0]["outcome"] == "ok"
    assert syncs[0]["detail"]["created"] == 6


def test_bundle_order_does_not_matter(ingest, container, bundle):
    reversed_bundle = dict(bundle)
    reversed_bundle["entry"] = list(reversed(bundle["entry"]))

    report = ingest.apply_bundle(reversed_bundle)

    assert report.created["Claim"] == 2
    assert container.store.get_claim("claim-1")["patient_id"] == "200001"


def test_unknown_resource_types_are_skipped_with_a_warning(ingest, bundle):
    bundle["entry"].append(
        {"resource": {"resourceType": "Encounter", "id": "enc-1", "status": "finished"}}
    )

    report = ingest.apply_bundle(bundle)

    assert report.skipped["Encounter"] == 1
    assert any(w["resource_type"] == "Encounter" for w in report.warnings)


def test_report_never_contains_clinical_text(ingest, bundle):
    report = ingest.apply_bundle(bundle)
    dumped = json.dumps(report.to_dict())

    assert "Ana" not in dumped
    assert "Silva" not in dumped


class FakeClient:
    def __init__(self, resources):
        self.resources = resources
        self.calls: list[str] = []

    def search(self, resource_type, params=None):
        self.calls.append(resource_type)
        return [r for r in self.resources if r["resourceType"] == resource_type]

    def source_label(self):
        return "fake://server"


def test_sync_pulls_in_dependency_order(container, bundle):
    ingest = FhirIngest(container.store, container.settings, audit=container.audit)
    client = FakeClient([e["resource"] for e in bundle["entry"]])

    report = ingest.sync(client)

    assert client.calls == ["Patient", "Coverage", "Appointment", "Claim"]
    assert report.created["Patient"] == 2
    assert container.store.get_patient("200001")["payer"] == "BlueShield"


def test_sync_rejects_unknown_resource_types(container, bundle):
    ingest = FhirIngest(container.store, container.settings)
    with pytest.raises(ValueError, match="unsupported"):
        ingest.sync(FakeClient([]), ["Observation"])
