from __future__ import annotations

from hospital_agent.tools.scoring import (
    capacity_pressure,
    claim_denial_risk,
    discharge_readiness,
    no_show_probability,
)


def test_no_show_increases_with_lead_time() -> None:
    short = no_show_probability(lead_days=1, prior_no_shows=0, appt_type="follow_up", payer="Aetna", distance_km=5)
    long = no_show_probability(lead_days=45, prior_no_shows=0, appt_type="follow_up", payer="Aetna", distance_km=5)
    assert long["probability"] > short["probability"]


def test_no_show_increases_with_prior_no_shows() -> None:
    base = no_show_probability(lead_days=7, prior_no_shows=0, appt_type="new", payer="Aetna", distance_km=5)
    repeat = no_show_probability(lead_days=7, prior_no_shows=3, appt_type="new", payer="Aetna", distance_km=5)
    assert repeat["probability"] > base["probability"]


def test_risk_is_explainable_and_bounded() -> None:
    result = no_show_probability(lead_days=90, prior_no_shows=9, appt_type="new", payer="SelfPay", distance_km=500)
    assert 0.0 <= result["probability"] <= 1.0
    assert result["drivers"]
    contributions = [d["contribution"] for d in result["drivers"]]
    assert contributions == sorted(contributions, reverse=True)
    assert result["recommendation"]


def test_discharge_readiness_orders_barriers() -> None:
    assert discharge_readiness([])["readiness"] == 1.0
    one = discharge_readiness(["awaiting_labs"])["readiness"]
    two = discharge_readiness(["awaiting_labs", "transport_arranged"])["readiness"]
    assert one > two
    assert discharge_readiness(["social_work_clearance"])["band"] == "nearly_ready"
    assert discharge_readiness(["social_work_clearance", "awaiting_imaging"])["band"] == "blocked"


def test_claim_flags_missing_auth_for_high_cost_procedure() -> None:
    claim = {"cpt": "27447", "icd10": "M17.11", "amount_cents": 3_250_000, "auth_on_file": 0, "payer": "Aetna"}
    result = claim_denial_risk(claim)
    codes = {f["code"] for f in result["findings"]}
    assert "AUTH_REQUIRED_BY_POLICY" in codes
    assert result["band"] == "high"
    assert all(f["fix"] for f in result["findings"])


def test_clean_claim_has_no_findings() -> None:
    claim = {"cpt": "99213", "icd10": "I10", "amount_cents": 12_500, "auth_on_file": 1, "payer": "Aetna"}
    result = claim_denial_risk(claim)
    assert result["cleared"] is True
    assert result["denial_risk"] < 0.25


def test_cpt_icd_mismatch_is_flagged() -> None:
    claim = {"cpt": "27447", "icd10": "K21.9", "amount_cents": 100_000, "auth_on_file": 1, "payer": "Aetna"}
    codes = {f["code"] for f in claim_denial_risk(claim)["findings"]}
    assert "CPT_ICD_MISMATCH" in codes


def test_capacity_pressure_bands() -> None:
    assert capacity_pressure(occupancy=0.5, boarders=0, ed_wait_hours=0)["band"] == "normal"
    assert capacity_pressure(occupancy=0.98, boarders=6, ed_wait_hours=9)["band"] in {"high", "critical"}
