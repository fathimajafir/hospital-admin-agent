"""Minimal FHIR R4 REST client.

Only what an ingest job needs: search with paging, read, create and the
CapabilityStatement. No model of the FHIR data types - resources stay plain
dicts, and interpretation lives in ``mapping``.

Two deliberate limits:

* ``same_origin`` is on. A server can otherwise hand us a ``next`` link to any
  host and we would follow it with the bearer token attached. That is a
  credential-leak path, not a convenience.
* ``max_pages`` bounds every search. An ingest job that pages forever is worse
  than one that stops and says so.
"""

from __future__ import annotations

from typing import Any, Iterable
from urllib.parse import urljoin, urlparse

import httpx

from .errors import FhirError

ACCEPT = "application/fhir+json"


def _issue_codes(body: Any) -> list[str]:
    if not isinstance(body, dict) or body.get("resourceType") != "OperationOutcome":
        return []
    codes = []
    for issue in body.get("issue") or []:
        code = issue.get("code")
        if isinstance(code, str):
            codes.append(code)
    return codes


def _diagnostics(body: Any) -> str | None:
    if not isinstance(body, dict):
        return None
    if body.get("resourceType") == "OperationOutcome":
        for issue in body.get("issue") or []:
            text = issue.get("diagnostics") or (issue.get("details") or {}).get("text")
            if text:
                return str(text)
        return "OperationOutcome without diagnostics"
    return None


class FhirClient:
    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        timeout_s: float = 30.0,
        page_size: int = 100,
        max_pages: int = 20,
        verify: bool = True,
        same_origin: bool = True,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not base_url:
            raise ValueError("base_url is required")
        self.base_url = base_url.rstrip("/")
        self.page_size = max(1, page_size)
        self.max_pages = max(1, max_pages)
        self.same_origin = same_origin
        origin = urlparse(self.base_url)
        self._origin = (origin.scheme, origin.netloc)

        headers = {"Accept": ACCEPT}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.Client(
            headers=headers,
            timeout=timeout_s,
            verify=verify,
            follow_redirects=False,
            transport=transport,
        )

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "FhirClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- transport ---------------------------------------------------------
    def _url(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        return f"{self.base_url}/{path.lstrip('/')}"

    def _allowed(self, url: str) -> bool:
        if not self.same_origin:
            return True
        parsed = urlparse(url)
        return (parsed.scheme, parsed.netloc) == self._origin

    def _json(self, response: httpx.Response, what: str) -> dict[str, Any]:
        if 300 <= response.status_code < 400:
            # Redirects are not followed: a 302 to another host would replay the
            # bearer token there. Point the operator at the right base URL instead.
            location = response.headers.get("location", "?")
            raise FhirError(
                f"{what} answered with HTTP {response.status_code} to {location}; "
                "set HA_FHIR_BASE_URL to the final address",
                status=response.status_code,
            )
        if response.status_code >= 400:
            try:
                body: Any = response.json()
            except ValueError:
                body = None
            detail = _diagnostics(body) or response.text[:200] or "no response body"
            raise FhirError(
                f"{what} failed: HTTP {response.status_code} {detail}",
                status=response.status_code,
                issue_codes=_issue_codes(body),
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise FhirError(f"{what} returned a non-JSON body") from exc
        if not isinstance(body, dict):
            raise FhirError(f"{what} returned a JSON {type(body).__name__}, expected an object")
        return body

    # -- operations --------------------------------------------------------
    def search(self, resource_type: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Collect every resource of one type, following Bundle ``next`` links.

        A server that answers with a bare resource or a ``collection`` Bundle is
        handled too, because real endpoints are inconsistent about this.
        """
        query = {"_count": str(self.page_size)}
        query.update({k: str(v) for k, v in (params or {}).items() if v is not None})

        resources: list[dict[str, Any]] = []
        url: str | None = self._url(resource_type)
        page = 0

        while url:
            page += 1
            if page > self.max_pages:
                raise FhirError(
                    f"stopped after {self.max_pages} pages of {resource_type}; "
                    "raise HA_FHIR_MAX_PAGES or narrow the search",
                    details={"resource_type": resource_type, "pages": self.max_pages},
                )
            body = self._json(self._client.get(url, params=query if page == 1 else None), f"{resource_type} search")

            if body.get("resourceType") != "Bundle":
                resources.append(body)
                break

            for entry in body.get("entry") or []:
                resource = (entry or {}).get("resource")
                if isinstance(resource, dict) and resource.get("resourceType") == resource_type:
                    resources.append(resource)

            url = self._next_link(body)

        return resources

    def _next_link(self, bundle: dict[str, Any]) -> str | None:
        for link in bundle.get("link") or []:
            if (link or {}).get("relation") == "next" and link.get("url"):
                target = urljoin(self.base_url + "/", str(link["url"]))
                if not self._allowed(target):
                    raise FhirError(
                        "refused to follow a paging link to a different origin",
                        details={"expected_origin": list(self._origin)},
                    )
                return target
        return None

    def read(self, resource_type: str, resource_id: str) -> dict[str, Any]:
        url = self._url(f"{resource_type}/{resource_id}")
        return self._json(self._client.get(url), f"{resource_type}/{resource_id} read")

    def create(self, resource_type: str, resource: dict[str, Any]) -> dict[str, Any]:
        response = self._client.post(
            self._url(resource_type),
            json=resource,
            headers={"Content-Type": ACCEPT},
        )
        return self._json(response, f"{resource_type} create")

    def capability(self) -> dict[str, Any]:
        return self._json(self._client.get(self._url("metadata")), "CapabilityStatement read")

    # -- introspection -----------------------------------------------------
    def source_label(self) -> str:
        """Host only: safe to put in an audit record or a report."""
        parsed = urlparse(self.base_url)
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def build_client(settings: Any, *, transport: httpx.BaseTransport | None = None) -> FhirClient:
    """Construct a client from Settings, or raise if FHIR is not configured."""
    from .errors import FhirNotConfigured

    if not getattr(settings, "fhir_base_url", ""):
        raise FhirNotConfigured()
    return FhirClient(
        settings.fhir_base_url,
        token=settings.fhir_token or None,
        timeout_s=settings.fhir_timeout_s,
        page_size=settings.fhir_page_size,
        max_pages=settings.fhir_max_pages,
        verify=settings.fhir_verify_tls,
        transport=transport,
    )


def iter_entries(bundle: dict[str, Any]) -> Iterable[dict[str, Any]]:
    for entry in bundle.get("entry") or []:
        resource = (entry or {}).get("resource")
        if isinstance(resource, dict):
            yield resource
