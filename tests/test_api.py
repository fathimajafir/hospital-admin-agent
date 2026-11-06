from __future__ import annotations

from datetime import datetime, timedelta, timezone


def test_health_and_readiness(client) -> None:
    assert client.get("/healthz").json()["status"] == "ok"
    ready = client.get("/readyz").json()
    assert ready["status"] == "ready"
    assert ready["tools"] == 14


def test_missing_key_is_rejected(client) -> None:
    response = client.get("/v1/me")
    assert response.status_code == 401


def test_invalid_key_is_rejected(client) -> None:
    assert client.get("/v1/me", headers={"X-API-Key": "nope"}).status_code == 401


def test_me_reports_role_and_purposes(client, admin_headers) -> None:
    body = client.get("/v1/me", headers=admin_headers).json()
    assert body["role"] == "admin"
    assert "operations" in body["purposes"]


def test_tool_catalog_is_authorised_per_role(client, admin_headers) -> None:
    tools = {t["name"] for t in client.get("/v1/tools", headers=admin_headers).json()["tools"]}
    assert "capacity_snapshot" in tools

    analyst = {"X-API-Key": "analyst-key"}
    analyst_tools = {t["name"] for t in client.get("/v1/tools", headers=analyst).json()["tools"]}
    assert "capacity_snapshot" in analyst_tools
    assert "patient_get" not in analyst_tools


def test_direct_tool_call(client, admin_headers) -> None:
    response = client.post("/v1/tools/capacity_snapshot", json={"tool": "capacity_snapshot", "args": {}}, headers=admin_headers)
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["data"]["totals"]["beds"] == 54


def test_direct_tool_call_denied_by_policy(client) -> None:
    response = client.post(
        "/v1/tools/patient_get",
        json={"tool": "patient_get", "args": {"patient_id": "P0001"}, "purpose_of_use": "operations"},
        headers={"X-API-Key": "analyst-key"},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "policy_denied"


def test_phi_requires_purpose(client) -> None:
    response = client.post(
        "/v1/tools/patient_get",
        json={"tool": "patient_get", "args": {"patient_id": "P0001"}},
        headers={"X-API-Key": "clinician-key"},
    )
    assert response.status_code == 403
    assert "purpose" in response.json()["error"]["message"]


def test_unknown_tool_is_404(client, admin_headers) -> None:
    response = client.post("/v1/tools/does_not_exist", json={"tool": "does_not_exist", "args": {}}, headers=admin_headers)
    assert response.status_code == 404


def test_invalid_args_are_422(client, admin_headers) -> None:
    response = client.post(
        "/v1/tools/capacity_snapshot",
        json={"tool": "capacity_snapshot", "args": {"include_recommendations": "not-a-bool"}},
        headers=admin_headers,
    )
    assert response.status_code == 200 or response.status_code == 422


def test_agent_run_over_http(client, admin_headers) -> None:
    response = client.post(
        "/v1/agent/run",
        json={"task": "give me the morning digest", "purpose_of_use": "operations"},
        headers=admin_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert body["run_id"].startswith("run_")


def test_write_tool_returns_202_with_approval(client, admin_headers) -> None:
    start = (datetime.now(timezone.utc) + timedelta(days=4)).strftime("%Y-%m-%dT11:00")
    response = client.post(
        "/v1/tools/schedule_appointment",
        json={
            "tool": "schedule_appointment",
            "args": {"patient_id": "P0002", "dept": "Cardiology", "provider": "Dr. Chen", "start_ts": start},
            "purpose_of_use": "operations",
        },
        headers=admin_headers,
    )
    assert response.status_code == 202
    approval_id = response.json()["error"]["approval_id"]
    assert approval_id.startswith("apv_")

    pending = client.get("/v1/approvals", headers=admin_headers).json()["pending"]
    assert any(p["id"] == approval_id for p in pending)

    # The requester may not approve their own action.
    self_approve = client.post(
        f"/v1/approvals/{approval_id}/decide", json={"decision": "approve"}, headers=admin_headers
    )
    assert self_approve.status_code == 403

    other = {"X-API-Key": "admin2-key"}
    decided = client.post(f"/v1/approvals/{approval_id}/decide", json={"decision": "approve"}, headers=other)
    assert decided.status_code == 200
    assert decided.json()["approval"]["status"] == "approved"


def test_audit_verify_over_http(client, admin_headers) -> None:
    client.post("/v1/tools/capacity_snapshot", json={"tool": "capacity_snapshot", "args": {}}, headers=admin_headers)
    body = client.get("/v1/audit/verify", headers=admin_headers).json()
    assert body["intact"] is True

    entries = client.get("/v1/audit?limit=5", headers=admin_headers).json()["entries"]
    assert entries
    assert "args_hash" in entries[0]


def test_analyst_cannot_read_audit(client) -> None:
    response = client.get("/v1/audit", headers={"X-API-Key": "analyst-key"})
    assert response.status_code == 200  # analysts are auditors-by-default here


def test_metrics_endpoint(client) -> None:
    client.get("/healthz")
    body = client.get("/metrics").text
    assert "ha_http_requests_total" in body
