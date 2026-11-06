"""Tool registry: the single choke point for every capability.

Nothing - neither the HTTP API, the agent loop, nor the MCP server - executes a
tool handler directly. It always goes through ``call``, which is what guarantees
that policy, approval, redaction, idempotency and audit cannot be bypassed by a
new caller.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from pydantic import BaseModel, ValidationError

from ..audit import AuditLog
from ..errors import ApprovalRequired, InvalidRequest, NotFound
from ..models import Principal, ToolResult, utcnow
from ..policy.engine import PHI_PERMISSION, PolicyEngine
from ..redact import redact_record


@dataclass
class ToolContext:
    store: Any
    principal: Principal
    purpose: str | None
    settings: Any
    run_id: str | None = None
    approval_id: str | None = None
    request_id: str | None = None


@dataclass
class ToolSpec:
    name: str
    description: str
    args_model: type[BaseModel]
    handler: Callable[[ToolContext, Any], dict[str, Any]]
    mutates: bool = False
    tags: tuple[str, ...] = field(default_factory=tuple)


class ToolRegistry:
    def __init__(self, *, policy: PolicyEngine, audit: AuditLog, settings: Any) -> None:
        self.policy = policy
        self.audit = audit
        self.settings = settings
        self._tools: dict[str, ToolSpec] = {}

    # -- registration ------------------------------------------------------
    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"tool '{spec.name}' already registered")
        self._tools[spec.name] = spec

    def register_all(self, specs: list[ToolSpec]) -> None:
        for spec in specs:
            self.register(spec)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def get(self, name: str) -> ToolSpec:
        spec = self._tools.get(name)
        if spec is None:
            raise NotFound(f"unknown tool '{name}'", details={"available": self.names()})
        return spec

    # -- introspection -----------------------------------------------------
    def schema(self, name: str) -> dict[str, Any]:
        return self.get(name).args_model.model_json_schema()

    def catalog(self, principal: Principal) -> list[dict[str, Any]]:
        """Only tools the caller could actually run, with JSON Schema inputs."""
        visible = []
        for name in self.names():
            decision = self.policy.check(principal, name, purpose=None, check_purpose=False)
            if not decision.allowed:
                continue
            spec = self._tools[name]
            visible.append(
                {
                    "name": name,
                    "description": spec.description,
                    "inputSchema": spec.args_model.model_json_schema(),
                    "mutates": spec.mutates,
                    "requires_approval": decision.requires_approval,
                    "tags": list(spec.tags),
                }
            )
        return visible

    # -- execution ---------------------------------------------------------
    def call(
        self,
        name: str,
        raw_args: dict[str, Any] | None,
        ctx: ToolContext,
        *,
        idempotency_key: str | None = None,
    ) -> ToolResult:
        started = time.perf_counter()
        spec = self.get(name)
        args_in = raw_args or {}

        decision = self.policy.enforce(ctx.principal, name, purpose=ctx.purpose)

        if decision.requires_approval and not ctx.approval_id:
            approval = self._open_approval(spec, args_in, ctx)
            self.audit.record(
                ctx.principal,
                action="approval.requested",
                outcome="pending",
                tool=name,
                args=args_in,
                detail={"approval_id": approval["id"], "run_id": ctx.run_id},
            )
            raise ApprovalRequired(
                f"'{name}' requires human approval before it can run",
                approval_id=approval["id"],
            )

        try:
            args = spec.args_model.model_validate(args_in)
        except ValidationError as exc:
            self.audit.record(
                ctx.principal,
                action="tool.validation_failed",
                outcome="error",
                tool=name,
                detail={"errors": len(exc.errors())},
            )
            raise InvalidRequest(
                f"invalid arguments for '{name}'",
                details={"errors": exc.errors(include_url=False)},
            ) from exc

        if idempotency_key and spec.mutates:
            cached = ctx.store.get_idempotent(idempotency_key)
            if cached is not None:
                return ToolResult.model_validate(cached)

        try:
            data = spec.handler(ctx, args)
        except (KeyError, ValueError) as exc:
            message = str(exc).strip("'\"")
            self.audit.record(
                ctx.principal, action="tool.call", outcome="error", tool=name, args=args_in,
                detail={"error": message},
            )
            raise (NotFound(message) if isinstance(exc, KeyError) else InvalidRequest(message)) from exc

        latency_ms = int((time.perf_counter() - started) * 1000)
        required = set(decision.tool_rule.get("permissions", []))
        allow_phi = PHI_PERMISSION in required and ctx.principal.has_purpose(ctx.purpose)
        safe_data, masked = redact_record(data if isinstance(data, dict) else {"value": data}, allow_phi=allow_phi)

        audit_id = self.audit.record(
            ctx.principal,
            action="tool.call",
            outcome="ok",
            tool=name,
            args=args_in,
            detail={"latency_ms": latency_ms, "masked": len(masked)},
        )

        result = ToolResult(
            ok=True,
            tool=name,
            data=safe_data,
            latency_ms=latency_ms,
            audit_id=audit_id,
            redactions=masked,
        )
        if idempotency_key and spec.mutates:
            ctx.store.put_idempotent(idempotency_key, name, result.model_dump())
        return result

    # -- approvals ---------------------------------------------------------
    def _open_approval(self, spec: ToolSpec, args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        created = datetime.now(timezone.utc)
        return ctx.store.create_approval(
            {
                "id": f"apv_{uuid.uuid4().hex[:12]}",
                "run_id": ctx.run_id or "direct",
                "tool": spec.name,
                "args": args,
                "requested_by": ctx.principal.sub,
                "created_at": created.isoformat(timespec="seconds"),
                "expires_at": (created + timedelta(seconds=self.settings.approval_ttl_s)).isoformat(timespec="seconds"),
            }
        )

    def execute_approval(self, approval_id: str, ctx: ToolContext) -> ToolResult:
        approval = ctx.store.get_approval(approval_id)
        if approval is None:
            raise NotFound(f"approval {approval_id} not found")
        if approval["status"] not in {"pending", "approved"}:
            raise InvalidRequest(
                f"approval {approval_id} is {approval['status']}; it can no longer be executed"
            )

        expires_at = datetime.fromisoformat(approval["expires_at"])
        if datetime.now(timezone.utc) > expires_at:
            ctx.store.decide_approval(approval_id, decision="expired", decided_by="system", reason="ttl elapsed")
            raise InvalidRequest(f"approval {approval_id} expired")

        approved_ctx = ToolContext(
            store=ctx.store,
            principal=ctx.principal,
            purpose=ctx.purpose,
            settings=ctx.settings,
            run_id=approval["run_id"],
            approval_id=approval_id,
            request_id=ctx.request_id,
        )
        result = self.call(approval["tool"], approval["args"], approved_ctx)
        # Terminal state: prevents a replay of the same approval from running twice.
        ctx.store.set_approval_status(approval_id, "executed")
        self.audit.record(
            ctx.principal,
            action="approval.executed",
            outcome="ok",
            tool=approval["tool"],
            detail={"approval_id": approval_id, "requested_by": approval["requested_by"]},
        )
        return result
