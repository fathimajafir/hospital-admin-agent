from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

import hospital_agent.fhir.writeback as writeback
from hospital_agent.config import Settings
from hospital_agent.container import Container
from hospital_agent.errors import ApprovalRequired
from hospital_agent.fhir import FhirClient
from hospital_agent.fhir.errors import FhirError
from hospital_agent.models import Principal, Role
from hospital_agent.store import Store
from hospital_agent.tools.registry import ToolContext

BASE = "https://fhir.example.org/r4"


def _start() -> str:
    when = (datetime.now(timezone.utc) + timedelta(days=3)).replace(hour=10, minute=0, second=0, microsecond=0)
    return when.isoformat(timespec="seconds")


def _settings(**overrides) -> Settings:
    values = dict(
        db_path=":memory:",
        fhir_base_url=BASE,
        fhir_token="secret-token",
        fhir_write_enabled=True,
        approval_ttl_s=600,
        rate_limit_rpm=10_000,
    )
    values.update(overrides)
    return Settings(**values)


def _container(settings: Settings) -> Container:
    return Container.build(settings, store=Store(":memory:"), configure_logs=False)


def _admin() -> Principal:
    return Principal(sub="u-admin", role=Role.admin, tenant="t1", purposes=("treatment", "operations", "payment"))


def _patch_transport(monkeypatch, handler) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    def fake_build(settings, *, transport=None):
        return FhirClient(settings.fhir_base_url, token=settings.fhir_token, transport=httpx.MockTransport(wrapped))

    monkeypatch.setattr(writeback, "build_client", fake_build)
    return seen


def _args() -> dict:
    return {
        "patient_id": "P0001",
        "dept": "Orthopedics",
        "provider": "Dr. Test",
        "start_ts": _start(),
        "appt_type": "new",
    }


def test_appointment_to_fhir_shape():
    payload = writeback.appointment_to_fhir(
        {
            "id": "A1",
            "patient_id": "P0001",
            "dept": "Orthopedics",
            "provider": "Dr. Rao",
            "start_ts": "2026-12-01T09:00:00+00:00",
            "appt_type": "follow_up",
        },
        {"id": "P0001", "mrn": "100001", "name": "Test Patient 01"},
        patient_reference="fhir-pat-1",
    )

    assert payload["resourceType"] == "Appointment"
    assert payload["status"] == "booked"
    assert payload["start"] == "2026-12-01T09:00:00+00:00"
    assert "end" not in payload  # duration is not modelled locally; not invented
    assert payload["participant"][0]["actor"]["reference"] == "Patient/fhir-pat-1"
    assert payload["participant"][1]["actor"]["display"] == "Dr. Rao"


def test_booking_writes_to_fhir_and_links_the_row(monkeypatch):
    container = _container(_settings())
    seen = _patch_transport(
        monkeypatch,
        lambda request: httpx.Response(201, json={"resourceType": "Appointment", "id": "srv-1"}),
    )
    ctx = ToolContext(
        store=container.store,
        principal=_admin(),
        purpose="operations",
        settings=container.settings,
        approval_id="apv-test",
    )

    result = container.registry.call("schedule_appointment", _args(), ctx)

    assert result.ok
    assert result.data["external"]["id"] == "srv-1"
    assert len(seen) == 1
    assert seen[0].method == "POST"
    assert seen[0].url.path.endswith("/Appointment")

    local_id = result.data["appointment"]["id"]
    assert container.store.get_appointment(local_id) is not None
    assert container.store.internal_for_fhir(BASE, "Appointment", "srv-1") == local_id


def test_writeback_is_off_by_default(monkeypatch):
    container = _container(_settings(fhir_write_enabled=False))
    seen = _patch_transport(monkeypatch, lambda request: httpx.Response(201, json={"id": "srv-1"}))
    ctx = ToolContext(
        store=container.store,
        principal=_admin(),
        purpose="operations",
        settings=container.settings,
        approval_id="apv-test",
    )

    result = container.registry.call("schedule_appointment", _args(), ctx)

    assert result.ok
    assert "external" not in result.data
    assert seen == []


def test_without_approval_nothing_reaches_the_ehr(monkeypatch):
    container = _container(_settings())
    seen = _patch_transport(monkeypatch, lambda request: httpx.Response(201, json={"id": "srv-1"}))
    ctx = ToolContext(store=container.store, principal=_admin(), purpose="operations", settings=container.settings)

    with pytest.raises(ApprovalRequired):
        container.registry.call("schedule_appointment", _args(), ctx)

    assert seen == []


def test_fhir_rejection_leaves_no_local_appointment(monkeypatch):
    container = _container(_settings())
    before = len(container.store.appointments(status=None))
    _patch_transport(
        monkeypatch,
        lambda request: httpx.Response(
            422,
            json={
                "resourceType": "OperationOutcome",
                "issue": [{"code": "invalid", "diagnostics": "start is in the past"}],
            },
        ),
    )
    ctx = ToolContext(
        store=container.store,
        principal=_admin(),
        purpose="operations",
        settings=container.settings,
        approval_id="apv-test",
    )

    with pytest.raises(FhirError, match="start is in the past"):
        container.registry.call("schedule_appointment", _args(), ctx)

    assert len(container.store.appointments(status=None)) == before
