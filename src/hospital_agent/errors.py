"""Typed errors.

Each maps to exactly one transport-level outcome, so HTTP and MCP clients behave
identically and nothing falls through to a generic 500.
"""

from __future__ import annotations

from typing import Any


class AgentError(Exception):
    code = "agent_error"
    http_status = 500

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


class PolicyDenied(AgentError):
    code = "policy_denied"
    http_status = 403


class ApprovalRequired(AgentError):
    code = "approval_required"
    http_status = 202

    def __init__(self, message: str, approval_id: str) -> None:
        super().__init__(message, details={"approval_id": approval_id})
        self.approval_id = approval_id


class NotFound(AgentError):
    code = "not_found"
    http_status = 404


class Conflict(AgentError):
    code = "conflict"
    http_status = 409


class BudgetExceeded(AgentError):
    code = "budget_exceeded"
    http_status = 429


class InvalidRequest(AgentError):
    code = "invalid_request"
    http_status = 422


class UpstreamError(AgentError):
    code = "upstream_error"
    http_status = 502
