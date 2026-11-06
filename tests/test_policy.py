from __future__ import annotations

import pytest

from hospital_agent.errors import PolicyDenied
from hospital_agent.models import Principal, Role
from hospital_agent.policy.engine import PolicyEngine


@pytest.fixture()
def policy() -> PolicyEngine:
    return PolicyEngine.from_file(None)


def test_analyst_cannot_read_phi(policy: PolicyEngine) -> None:
    analyst = Principal(sub="a", role=Role.analyst, purposes=("operations",))
    decision = policy.check(analyst, "patient_get", purpose="operations")
    assert decision.allowed is False
    assert "phi" in decision.missing_permissions


def test_clinician_needs_purpose_for_phi(policy: PolicyEngine) -> None:
    clinician = Principal(sub="c", role=Role.clinician, purposes=("treatment",))
    assert policy.check(clinician, "patient_get", purpose=None).allowed is False
    assert policy.check(clinician, "patient_get", purpose="treatment").allowed is True


def test_purpose_must_be_granted_to_the_principal(policy: PolicyEngine) -> None:
    clinician = Principal(sub="c", role=Role.clinician, purposes=("treatment",))
    decision = policy.check(clinician, "patient_get", purpose="research")
    assert decision.allowed is False
    assert "not granted" in decision.reason


def test_unknown_purpose_rejected(policy: PolicyEngine) -> None:
    clinician = Principal(sub="c", role=Role.clinician, purposes=("whatever",))
    decision = policy.check(clinician, "patient_get", purpose="whatever")
    assert decision.allowed is False


def test_write_tools_require_approval(policy: PolicyEngine) -> None:
    admin = Principal(sub="a", role=Role.admin, purposes=("operations",))
    decision = policy.check(admin, "schedule_appointment", purpose="operations")
    assert decision.allowed is True
    assert decision.requires_approval is True


def test_deny_by_default_for_unknown_tool(policy: PolicyEngine) -> None:
    analyst = Principal(sub="a", role=Role.analyst, purposes=("operations",))
    assert policy.check(analyst, "give_me_everything", purpose="operations").allowed is False


def test_enforce_raises(policy: PolicyEngine) -> None:
    analyst = Principal(sub="a", role=Role.analyst, purposes=("operations",))
    with pytest.raises(PolicyDenied):
        policy.enforce(analyst, "patient_get", purpose="operations")


def test_admin_wildcard_expands(policy: PolicyEngine) -> None:
    admin = Principal(sub="a", role=Role.admin, purposes=("treatment", "operations"))
    perms = policy.permissions_for_role("admin")
    assert {"read", "write", "phi", "financial", "clinical", "schedule"} <= perms


def test_catalog_is_filtered_per_role(policy: PolicyEngine) -> None:
    all_tools = ["capacity_snapshot", "patient_get", "schedule_appointment", "billing_summary"]
    analyst = Principal(sub="a", role=Role.analyst, purposes=("operations",))
    assert policy.visible_tools(analyst, all_tools) == ["capacity_snapshot"]
