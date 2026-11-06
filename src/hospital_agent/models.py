"""Shared contracts: principals, tool calls, agent runs, approvals.

These are the only types that cross a process boundary (HTTP or MCP), so they are
deliberately explicit and validated with ``extra="forbid"``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Role(str, Enum):
    service = "service"
    analyst = "analyst"
    scheduler = "scheduler"
    billing = "billing"
    clinician = "clinician"
    admin = "admin"


#: Purposes recognised by the policy engine. Anything outside this set is rejected
#: rather than treated as "no purpose", so a typo cannot silently widen access.
PURPOSES = frozenset({"treatment", "operations", "payment", "research"})


class Principal(BaseModel):
    """The authenticated caller. Never contains credentials."""

    model_config = ConfigDict(frozen=True)

    sub: str
    role: Role
    tenant: str = "default"
    purposes: tuple[str, ...] = ()

    def has_purpose(self, purpose: str | None) -> bool:
        return purpose is not None and purpose in self.purposes


class ToolCallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    purpose_of_use: str | None = None
    idempotency_key: str | None = Field(default=None, max_length=128)


class ToolResult(BaseModel):
    ok: bool
    tool: str
    data: Any = None
    error: dict[str, Any] | None = None
    latency_ms: int = 0
    audit_id: int | None = None
    redactions: list[str] = Field(default_factory=list)


class StepKind(str, Enum):
    plan = "plan"
    tool = "tool"
    observation = "observation"
    approval = "approval"
    final = "final"
    error = "error"


class Step(BaseModel):
    index: int
    kind: StepKind
    summary: str
    tool: str | None = None
    args: dict[str, Any] | None = None
    ok: bool | None = None
    latency_ms: int = 0


class Usage(BaseModel):
    steps: int = 0
    tool_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    estimated_cost_usd: float = 0.0


class AgentRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: str = Field(min_length=3, max_length=4000)
    purpose_of_use: str | None = None
    max_steps: int | None = Field(default=None, ge=1, le=20)
    session_id: str | None = Field(default=None, max_length=128)
    context: dict[str, Any] = Field(default_factory=dict)


class RunStatus(str, Enum):
    completed = "completed"
    awaiting_approval = "awaiting_approval"
    failed = "failed"
    budget_exceeded = "budget_exceeded"


class AgentRunResponse(BaseModel):
    run_id: str
    status: RunStatus
    answer: str
    steps: list[Step] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    approval: "ApprovalRecord | None" = None
    error: str | None = None


class ApprovalRecord(BaseModel):
    id: str
    run_id: str
    tool: str
    args: dict[str, Any]
    requested_by: str
    status: Literal["pending", "approved", "denied", "expired", "executed"]
    created_at: str
    expires_at: str
    decided_by: str | None = None
    reason: str | None = None


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "deny"]
    reason: str = Field(default="", max_length=500)


class AuditEntry(BaseModel):
    id: int
    ts: str
    actor: str
    role: str
    tenant: str
    action: str
    tool: str | None = None
    outcome: str
    detail: dict[str, Any]
    prev_hash: str
    hash: str


AgentRunResponse.model_rebuild()
