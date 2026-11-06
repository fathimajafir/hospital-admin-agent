"""HTTP surface.

Thin by design: every route either reads state or delegates to the registry or
the agent. Business rules live below this layer so the MCP server exposes exactly
the same behaviour.
"""

from __future__ import annotations

import asyncio
import json
import queue
import threading
import uuid
from typing import Any

from fastapi import Depends, FastAPI, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse

from .. import metrics
from ..agent.core import HospitalAgent
from ..container import Container
from ..errors import AgentError, ApprovalRequired, NotFound, PolicyDenied
from ..logging_setup import get_logger
from ..models import (
    AgentRunRequest,
    AgentRunResponse,
    ApprovalDecision,
    ApprovalRecord,
    Principal,
    Step,
    ToolCallRequest,
    utcnow,
)
from ..tools.registry import ToolContext
from .security import RateLimiter, principal_from_request, require_role

log = get_logger("hospital_agent.api")


def create_app(container: Container | None = None) -> FastAPI:
    container = container or Container.build()

    app = FastAPI(
        title="Hospital Administration Agent",
        version="1.0.0",
        description=(
            "Agent + tool platform for hospital operations. Every capability is available "
            "both as an agent task and as a direct tool call, so the MCP server and the HTTP "
            "API cannot drift apart."
        ),
        docs_url="/docs",
        openapi_url="/openapi.json",
    )
    app.state.container = container
    app.state.limiter = RateLimiter(container.settings.rate_limit_rpm)
    app.state.started_at = utcnow()

    # -- middleware --------------------------------------------------------
    @app.middleware("http")
    async def request_context(request: Request, call_next: Any) -> Response:
        request_id = request.headers.get("X-Request-ID") or f"req_{uuid.uuid4().hex[:12]}"
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > container.settings.max_body_bytes:
            metrics.inc("ha_http_requests_total", status="413")
            return JSONResponse(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                content={"error": {"code": "payload_too_large", "message": "request body too large"}},
            )
        try:
            response = await call_next(request)
        except Exception:  # pragma: no cover - defensive
            metrics.inc("ha_http_requests_total", status="500")
            log.exception("unhandled error", extra={"request_id": request_id, "path": request.url.path})
            raise
        response.headers["X-Request-ID"] = request_id
        metrics.inc("ha_http_requests_total", status=str(response.status_code), path=request.url.path)
        return response

    # -- error handling ----------------------------------------------------
    @app.exception_handler(AgentError)
    async def agent_error_handler(request: Request, exc: AgentError) -> JSONResponse:
        body: dict[str, Any] = {"error": exc.to_dict(), "request_id": request.headers.get("X-Request-ID")}
        if isinstance(exc, ApprovalRequired):
            body["error"]["approval_id"] = exc.approval_id
        metrics.inc("ha_errors_total", code=exc.code)
        return JSONResponse(status_code=exc.http_status, content=body)

    @app.exception_handler(PolicyDenied)
    async def policy_handler(request: Request, exc: PolicyDenied) -> JSONResponse:
        metrics.inc("ha_errors_total", code="policy_denied")
        return JSONResponse(status_code=403, content={"error": exc.to_dict()})

    # -- health ------------------------------------------------------------
    @app.get("/healthz", tags=["ops"])
    async def healthz() -> dict[str, Any]:
        return {"status": "ok", "service": "hospital-admin-agent", "version": "1.0.0"}

    @app.get("/readyz", tags=["ops"])
    async def readyz() -> dict[str, Any]:
        try:
            container.store.beds()
        except Exception as exc:  # pragma: no cover - storage failure
            return JSONResponse(status_code=503, content={"status": "not_ready", "reason": str(exc)})
        return {
            "status": "ready",
            "started_at": app.state.started_at,
            "llm_provider": container.settings.llm_provider,
            "tools": len(container.registry.names()),
        }

    @app.get("/metrics", tags=["ops"])
    async def prometheus() -> Response:
        return Response(content=metrics.render(), media_type="text/plain; version=0.0.4")

    # -- identity ----------------------------------------------------------
    @app.get("/v1/me", tags=["identity"])
    async def me(principal: Principal = Depends(principal_from_request)) -> dict[str, Any]:
        return {
            "sub": principal.sub,
            "role": principal.role.value,
            "tenant": principal.tenant,
            "purposes": list(principal.purposes),
            "tools_available": len(container.registry.catalog(principal)),
        }

    # -- tools -------------------------------------------------------------
    @app.get("/v1/tools", tags=["tools"])
    async def list_tools(principal: Principal = Depends(principal_from_request)) -> dict[str, Any]:
        return {"tools": container.registry.catalog(principal)}

    @app.post("/v1/tools/{tool_name}", tags=["tools"])
    async def call_tool(
        tool_name: str,
        body: ToolCallRequest,
        principal: Principal = Depends(principal_from_request),
    ) -> JSONResponse:
        ctx = ToolContext(
            store=container.store,
            principal=principal,
            purpose=body.purpose_of_use,
            settings=container.settings,
        )
        result = container.registry.call(
            tool_name,
            body.args,
            ctx,
            idempotency_key=body.idempotency_key,
        )
        metrics.inc("ha_tool_calls_total", tool=tool_name, outcome="ok")
        return JSONResponse(status_code=200, content=result.model_dump(mode="json"))

    # -- agent -------------------------------------------------------------
    @app.post("/v1/agent/run", response_model=AgentRunResponse, tags=["agent"])
    async def run_agent(
        body: AgentRunRequest,
        principal: Principal = Depends(principal_from_request),
    ) -> AgentRunResponse:
        response = await asyncio.to_thread(container.agent.run, body, principal)
        metrics.inc("ha_agent_runs_total", status=response.status.value)
        return response

    @app.post("/v1/agent/stream", tags=["agent"])
    async def stream_agent(
        body: AgentRunRequest,
        principal: Principal = Depends(principal_from_request),
    ) -> StreamingResponse:
        """Server-sent events: one event per agent step, then a terminal event."""
        events: queue.Queue[tuple[str, Any]] = queue.Queue()

        def worker() -> None:
            def on_step(step: Step) -> None:
                events.put(("step", step.model_dump(mode="json")))

            try:
                response = container.agent.run(body, principal, on_step=on_step)
                events.put(("result", response.model_dump(mode="json")))
            except Exception as exc:  # surfaced to the client, never swallowed
                events.put(("error", {"message": str(exc)}))
            finally:
                events.put(("done", None))

        threading.Thread(target=worker, daemon=True, name="agent-sse").start()

        async def generator():
            loop = asyncio.get_running_loop()
            while True:
                kind, payload = await loop.run_in_executor(None, events.get)
                if kind == "done":
                    break
                yield f"event: {kind}\ndata: {json.dumps(payload, default=str)}\n\n"

        return StreamingResponse(
            generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/v1/runs/{run_id}", tags=["agent"])
    async def get_run(run_id: str, principal: Principal = Depends(principal_from_request)) -> dict[str, Any]:
        record = container.store.get_run(run_id)
        if record is None or record["principal_sub"] != principal.sub:
            raise NotFound(f"run {run_id} not found")
        record.pop("messages", None)  # internal working memory
        return record

    @app.post("/v1/runs/{run_id}/resume", response_model=AgentRunResponse, tags=["agent"])
    async def resume_run(
        run_id: str,
        approval_id: str,
        principal: Principal = Depends(principal_from_request),
    ) -> AgentRunResponse:
        response = await asyncio.to_thread(container.agent.resume, run_id, principal, approval_id)
        metrics.inc("ha_agent_runs_total", status=response.status.value)
        return response

    # -- approvals ---------------------------------------------------------
    @app.get("/v1/approvals", tags=["approvals"])
    async def list_approvals(principal: Principal = Depends(principal_from_request)) -> dict[str, Any]:
        require_role(principal, "clinician", "scheduler", "billing", "analyst")
        pending = container.store.pending_approvals()
        return {"pending": [ApprovalRecord(**_approval_fields(p)) for p in pending if p]}

    @app.post("/v1/approvals/{approval_id}/decide", tags=["approvals"])
    async def decide_approval(
        approval_id: str,
        body: ApprovalDecision,
        principal: Principal = Depends(principal_from_request),
    ) -> dict[str, Any]:
        require_role(principal, "clinician", "scheduler", "billing")
        approval = container.store.get_approval(approval_id)
        if approval is None:
            raise NotFound(f"approval {approval_id} not found")
        if approval["requested_by"] == principal.sub:
            raise PolicyDenied("approvals must be decided by a different person than the requester")

        record = container.store.decide_approval(
            approval_id,
            decision="approved" if body.decision == "approve" else "denied",
            decided_by=principal.sub,
            reason=body.reason,
        )
        container.audit.record(
            principal,
            action=f"approval.{body.decision}",
            outcome=record["status"],
            tool=record["tool"],
            detail={"approval_id": approval_id, "requested_by": record["requested_by"]},
        )
        metrics.inc("ha_approvals_total", decision=body.decision)
        return {"approval": ApprovalRecord(**_approval_fields(record))}

    # -- audit -------------------------------------------------------------
    @app.get("/v1/audit", tags=["audit"])
    async def list_audit(
        limit: int = 50,
        principal: Principal = Depends(principal_from_request),
    ) -> dict[str, Any]:
        require_role(principal, "clinician", "analyst", "billing")
        return {"entries": container.store.list_audit(limit=min(max(limit, 1), 500))}

    @app.get("/v1/audit/verify", tags=["audit"])
    async def verify_audit(principal: Principal = Depends(principal_from_request)) -> dict[str, Any]:
        require_role(principal, "clinician", "analyst", "billing")
        ok, reason = container.audit.verify()
        return {"intact": ok, "detail": reason, "checked_at": utcnow()}

    return app


def _approval_fields(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": record["id"],
        "run_id": record["run_id"],
        "tool": record["tool"],
        "args": record["args"],
        "requested_by": record["requested_by"],
        "status": record["status"],
        "created_at": record["created_at"],
        "expires_at": record["expires_at"],
        "decided_by": record.get("decided_by"),
        "reason": record.get("reason"),
    }


app = create_app()
