"""FHIR-specific failures.

They subclass ``AgentError`` so the HTTP layer renders them as 502/500 rather
than a bare 500, and so an MCP client sees the same shape it sees everywhere
else. Messages never contain the bearer token or the raw request headers.
"""

from __future__ import annotations

from typing import Any

from ..errors import AgentError, UpstreamError


class FhirError(UpstreamError):
    """The FHIR server refused, failed or returned something unusable."""

    code = "fhir_error"

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        issue_codes: list[str] | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        body = dict(details or {})
        if status is not None:
            body["upstream_status"] = status
        if issue_codes:
            body["issue_codes"] = issue_codes
        super().__init__(message, details=body)
        self.status = status
        self.issue_codes = issue_codes or []


class FhirNotConfigured(AgentError):
    """No FHIR base URL is set, so there is nothing to talk to."""

    code = "fhir_not_configured"
    http_status = 503

    def __init__(self, message: str = "HA_FHIR_BASE_URL is not set") -> None:
        super().__init__(message)
