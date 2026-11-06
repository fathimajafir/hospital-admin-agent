from __future__ import annotations

import pytest

from hospital_agent.errors import ApprovalRequired, NotFound, PolicyDenied
from hospital_agent.models import Principal, Role
from hospital_agent.tools.registry import ToolContext


def ctx(container, principal: Principal, purpose: str = "operations") -> ToolContext:
    return ToolContext(store=container.store, principal=principal, purpose=purpose, settings=container.settings)


def test_capacity_snapshot_reports_hospital_totals(container, admin) -> None:
    result = container.registry.call("capacity_snapshot", {}, ctx(container, admin))
    assert result.ok
    assert result.data["totals"]["beds"] == 54
    assert result.data["totals"]["occupied"] == 44
    assert 0.0 <= result.data["pressure"]["pressure"] <= 1.0
    assert len(result.data["wards"]) == 6
    # Sorted worst-first.
    pressures = [w["pressure"] for w in result.data["wards"]]
    assert pressures == sorted(pressures, reverse=True)


def test_capacity_snapshot_rejects_unknown_ward(container, admin) -> None:
    with pytest.raises(Exception) as exc:
        container.registry.call("capacity_snapshot", {"ward": "NOPE"}, ctx(container, admin))
    assert "not found" in str(exc.value)


def test_discharge_board_scores_barriers(container, admin) -> None:
    result = container.registry.call("discharge_board", {}, ctx(container, admin))
    assert result.data["count"] == 10
    for plan in result.data["plans"]:
        assert 0.0 <= plan["readiness"] <= 1.0
        assert "next_actions" in plan


def test_staffing_gaps_find_the_icu_night_gap(container, admin) -> None:
    result = container.registry.call("staffing_gaps", {"shift": "night"}, ctx(container, admin))
    icu = next(s for s in result.data["shifts"] if s["ward"] == "ICU")
    assert icu["gap_rn"] >= 1
    assert icu["severity"] in {"critical", "moderate"}


def test_claim_scrub_flags_missing_authorisation(container, admin) -> None:
    result = container.registry.call("claim_scrub", {"claim_id": "C0003"}, ctx(container, admin))
    codes = {f["code"] for f in result.data["findings"]}
    assert "AUTH_REQUIRED_BY_POLICY" in codes
    assert result.data["band"] == "high"


def test_claim_scrub_unknown_claim(container, admin) -> None:
    with pytest.raises(NotFound):
        container.registry.call("claim_scrub", {"claim_id": "C9999"}, ctx(container, admin))


def test_prior_auth_packet_lists_missing_documents(container, admin) -> None:
    result = container.registry.call(
        "build_prior_auth_packet", {"patient_id": "P0003", "cpt": "27447"}, ctx(container, admin, "payment")
    )
    assert result.data["ready_to_submit"] is False
    assert "prior_auth" in result.data["missing"]
    assert result.data["draft_justification"]


def test_patient_phi_visible_to_clinician_with_purpose(container, clinician) -> None:
    result = container.registry.call("patient_get", {"patient_id": "P0005"}, ctx(container, clinician, "treatment"))
    assert result.data["name"] == "Test Patient 05"
    assert result.redactions == []


def test_patient_phi_denied_to_analyst(container, analyst) -> None:
    with pytest.raises(PolicyDenied):
        container.registry.call("patient_get", {"patient_id": "P0005"}, ctx(container, analyst))


def test_phi_is_masked_when_purpose_policy_is_off(container) -> None:
    from hospital_agent.policy.engine import PolicyEngine

    container.registry.policy = PolicyEngine.from_file(None, require_phi_purpose=False)
    principal = Principal(sub="x", role=Role.clinician, purposes=())
    result = container.registry.call("patient_get", {"patient_id": "P0005"}, ctx(container, principal, ""))
    assert result.data["name"] == "T*** P***"
    assert "name" in result.redactions
    assert result.data["dob"].endswith("-**-**")


def test_write_tool_opens_an_approval(container, admin) -> None:
    with pytest.raises(ApprovalRequired) as exc:
        container.registry.call(
            "bed_assign", {"patient_id": "P0001", "bed_id": "A09"}, ctx(container, admin, "operations")
        )
    approval_id = exc.value.approval_id
    approval = container.store.get_approval(approval_id)
    assert approval["status"] == "pending"
    assert approval["tool"] == "bed_assign"


def test_approved_write_executes_once(container, admin) -> None:
    with pytest.raises(ApprovalRequired) as exc:
        container.registry.call(
            "bed_assign", {"patient_id": "P0001", "bed_id": "A09"}, ctx(container, admin, "operations")
        )
    approval_id = exc.value.approval_id
    container.store.decide_approval(approval_id, decision="approved", decided_by="u-admin2", reason="ok")
    result = container.registry.execute_approval(approval_id, ctx(container, admin))
    assert result.data["bed"]["patient_id"] == "P0001"
    assert container.store.get_bed("A09")["status"] == "occupied"


def test_idempotency_returns_the_first_result(container, admin) -> None:
    """A retried write with the same key must not run the handler again."""
    approved = ToolContext(
        store=container.store,
        principal=admin,
        purpose="operations",
        settings=container.settings,
        approval_id="test-bypass",
    )
    args = {"patient_id": "P0007", "dept": "Cardiology", "provider": "Dr. Rao", "start_ts": "2027-01-04T10:00"}

    first = container.registry.call("schedule_appointment", args, approved, idempotency_key="key-1")
    second = container.registry.call("schedule_appointment", args, approved, idempotency_key="key-1")

    assert first.ok and second.ok
    assert first.data == second.data
    # Only one appointment exists: the replay did not hit the handler.
    assert len([a for a in container.store.appointments() if a["start_ts"].startswith("2027-01-04")]) == 1


def test_audit_chain_detects_tampering(container, admin) -> None:
    container.registry.call("capacity_snapshot", {}, ctx(container, admin))
    ok, _ = container.audit.verify()
    assert ok

    container.store._exec("UPDATE audit SET outcome='forged' WHERE id=1")
    ok, detail = container.audit.verify()
    assert ok is False
    assert "mismatch" in detail or "break" in detail


def test_ops_digest_prioritises_actions(container, admin) -> None:
    result = container.registry.call("ops_digest", {}, ctx(container, admin))
    actions = result.data["top_actions"]
    assert actions
    assert actions[0]["priority"] == 1
    assert all(a["owner"] and a["impact"] for a in actions)


def test_tool_catalog_hides_forbidden_tools(container, analyst) -> None:
    names = {t["name"] for t in container.registry.catalog(analyst)}
    assert "capacity_snapshot" in names
    assert "patient_get" not in names
    assert "schedule_appointment" not in names
