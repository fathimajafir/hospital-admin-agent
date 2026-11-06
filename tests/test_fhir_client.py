from __future__ import annotations

import httpx
import pytest

from hospital_agent.fhir import FhirClient
from hospital_agent.fhir.errors import FhirError

BASE = "https://fhir.example.org/r4"


def _client(handler, **kwargs) -> FhirClient:
    return FhirClient(BASE, token="secret-token", transport=httpx.MockTransport(handler), **kwargs)


def test_search_sends_fhir_headers_and_count():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["accept"] = request.headers.get("accept")
        seen["auth"] = request.headers.get("authorization")
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"resourceType": "Bundle", "entry": []})

    with _client(handler) as client:
        assert client.search("Patient") == []

    assert seen["accept"] == "application/fhir+json"
    assert seen["auth"] == "Bearer secret-token"
    assert "_count=100" in str(seen["url"])


def test_search_follows_next_links():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("_page") == "2":
            return httpx.Response(
                200,
                json={
                    "resourceType": "Bundle",
                    "entry": [{"resource": {"resourceType": "Patient", "id": "p3"}}],
                },
            )
        return httpx.Response(
            200,
            json={
                "resourceType": "Bundle",
                "entry": [
                    {"resource": {"resourceType": "Patient", "id": "p1"}},
                    {"resource": {"resourceType": "Patient", "id": "p2"}},
                ],
                "link": [{"relation": "next", "url": f"{BASE}/Patient?_page=2"}],
            },
        )

    with _client(handler) as client:
        resources = client.search("Patient")

    assert [r["id"] for r in resources] == ["p1", "p2", "p3"]


def test_search_stops_at_max_pages():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "resourceType": "Bundle",
                "entry": [],
                "link": [{"relation": "next", "url": f"{BASE}/Patient"}],
            },
        )

    with _client(handler, max_pages=3) as client:
        with pytest.raises(FhirError, match="pages"):
            client.search("Patient")


def test_cross_origin_paging_link_is_refused():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "resourceType": "Bundle",
                "entry": [],
                "link": [{"relation": "next", "url": "https://evil.example.net/steal"}],
            },
        )

    with _client(handler) as client:
        with pytest.raises(FhirError, match="different origin"):
            client.search("Patient")


def test_operation_outcome_becomes_a_typed_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "resourceType": "OperationOutcome",
                "issue": [{"severity": "error", "code": "forbidden", "diagnostics": "missing scope patient/*.read"}],
            },
        )

    with _client(handler) as client:
        with pytest.raises(FhirError) as excinfo:
            client.search("Patient")

    error = excinfo.value
    assert error.status == 403
    assert "missing scope" in str(error)
    assert error.issue_codes == ["forbidden"]
    assert "secret-token" not in str(error)


def test_create_posts_fhir_json():
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["body"] = request.content
        captured["content_type"] = request.headers.get("content-type")
        return httpx.Response(201, json={"resourceType": "Appointment", "id": "srv-1"})

    with _client(handler) as client:
        created = client.create("Appointment", {"resourceType": "Appointment", "status": "booked"})

    assert created["id"] == "srv-1"
    assert captured["method"] == "POST"
    assert captured["content_type"] == "application/fhir+json"
    assert b'"status":"booked"' in captured["body"]


def test_non_json_body_is_reported_as_such():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>maintenance</html>")

    with _client(handler) as client:
        with pytest.raises(FhirError, match="non-JSON"):
            client.capability()
