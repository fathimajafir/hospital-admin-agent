"""The agent loop.

Bounded by three independent limits - steps, tokens, wall clock - because agents
fail by looping, not by being wrong once. Every tool call goes through the
registry, so a run inherits policy, approval gates, redaction and audit for free.

State is persisted after every terminal transition, which is what makes
``awaiting_approval`` a resumable state rather than a dead end.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from time import monotonic
from typing import Any, Callable

from ..audit import AuditLog
from ..errors import AgentError, ApprovalRequired, BudgetExceeded, UpstreamError
from ..logging_setup import get_logger
from ..models import (
    AgentRunRequest,
    AgentRunResponse,
    ApprovalRecord,
    Principal,
    RunStatus,
    Step,
    StepKind,
    Usage,
    utcnow,
)
from ..tools.registry import ToolContext, ToolRegistry
from ..llm.base import AskHuman, Decision, Final, ToolCall, UsageMeter
from .prompts import observation

log = get_logger("hospital_agent.agent")


@dataclass
class _RunState:
    id: str
    task: str
    session_id: str | None
    purpose: str | None
    messages: list[dict[str, str]] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    approval_id: str | None = None
    started_at: float = field(default_factory=monotonic)


class HospitalAgent:
    def __init__(
        self,
        *,
        store: Any,
        registry: ToolRegistry,
        planner_factory: Callable[[UsageMeter], Any],
        settings: Any,
        audit: AuditLog,
    ) -> None:
        self.store = store
        self.registry = registry
        self.planner_factory = planner_factory
        self.settings = settings
        self.audit = audit

    # -- public API --------------------------------------------------------
    def run(
        self,
        request: AgentRunRequest,
        principal: Principal,
        on_step: Callable[[Step], None] | None = None,
    ) -> AgentRunResponse:
        state = _RunState(
            id=f"run_{uuid.uuid4().hex[:16]}",
            task=request.task,
            session_id=request.session_id,
            purpose=request.purpose_of_use,
            messages=[{"role": "user", "content": request.task}],
        )
        self.audit.record(
            principal,
            action="agent.run.start",
            outcome="ok",
            detail={"run_id": state.id, "task_chars": len(request.task), "purpose": request.purpose_of_use},
        )
        response = self._loop(state, principal, request.max_steps, on_step)
        self._persist(state, principal, response)
        return response

    def resume(self, run_id: str, principal: Principal, approval_id: str) -> AgentRunResponse:
        record = self.store.get_run(run_id)
        if record is None:
            raise AgentError(f"run {run_id} not found")
        if record["principal_sub"] != principal.sub:
            # Do not disclose that the run exists to a different principal.
            raise AgentError(f"run {run_id} not found")

        state = _RunState(
            id=record["id"],
            task=record["task"],
            session_id=record["session_id"],
            purpose=record["purpose"],
            messages=record["messages"],
            steps=[Step.model_validate(s) for s in record["steps"]],
            usage=Usage.model_validate(record["usage"] or {}),
            approval_id=approval_id,
        )

        ctx = ToolContext(
            store=self.store,
            principal=principal,
            purpose=state.purpose,
            settings=self.settings,
            run_id=state.id,
            approval_id=approval_id,
        )
        approval = self.store.get_approval(approval_id)
        if approval is None:
            raise AgentError(f"approval {approval_id} not found")

        try:
            result = self.registry.execute_approval(approval_id, ctx)
            state.messages.append({"role": "tool", "content": observation(approval["tool"], True, result.data)})
            state.steps.append(
                Step(
                    index=len(state.steps),
                    kind=StepKind.observation,
                    summary=f"{approval['tool']} executed after approval",
                    tool=approval["tool"],
                    ok=True,
                    latency_ms=result.latency_ms,
                )
            )
            state.usage.tool_calls += 1
        except AgentError as exc:
            state.messages.append({"role": "tool", "content": observation(approval["tool"], False, exc.to_dict())})
            state.steps.append(
                Step(index=len(state.steps), kind=StepKind.error, summary=exc.message, tool=approval["tool"], ok=False)
            )

        response = self._loop(state, principal, None)
        self._persist(state, principal, response)
        return response

    # -- loop --------------------------------------------------------------
    def _loop(
        self,
        state: _RunState,
        principal: Principal,
        max_steps_override: int | None,
        on_step: Callable[[Step], None] | None = None,
    ) -> AgentRunResponse:
        meter = UsageMeter()
        meter.prompt_tokens = state.usage.prompt_tokens
        meter.completion_tokens = state.usage.completion_tokens
        planner = self.planner_factory(meter)
        tools = self.registry.catalog(principal)
        max_steps = min(max_steps_override or self.settings.max_steps, 20)
        deadline = state.started_at + self.settings.run_timeout_s

        ctx = ToolContext(
            store=self.store,
            principal=principal,
            purpose=state.purpose,
            settings=self.settings,
            run_id=state.id,
            approval_id=state.approval_id,
        )

        def record(step: Step) -> None:
            """Append a step and, if streaming, hand it to the caller immediately."""
            state.steps.append(step)
            if on_step is not None:
                try:
                    on_step(step)
                except Exception:  # a broken consumer must never fail the run
                    log.debug("on_step callback raised")

        while state.usage.steps < max_steps:
            if monotonic() > deadline:
                return self._finish(
                    state,
                    RunStatus.budget_exceeded,
                    "Stopped: the run exceeded its time budget. Narrow the question and try again.",
                )
            if meter.total > self.settings.token_budget:
                return self._finish(
                    state,
                    RunStatus.budget_exceeded,
                    "Stopped: the run exceeded its token budget. Narrow the question and try again.",
                )

            remaining = max_steps - state.usage.steps
            try:
                decision: Decision = planner.plan(
                    task=state.task,
                    messages=state.messages,
                    tools=tools,
                    remaining_steps=remaining,
                )
            except UpstreamError as exc:
                return self._finish(state, RunStatus.failed, f"The planning model is unavailable: {exc.message}")

            if isinstance(decision, Final):
                record(
                    Step(index=len(state.steps), kind=StepKind.final, summary=decision.text[:300])
                )
                return self._finish(state, RunStatus.completed, decision.text, meter)

            if isinstance(decision, AskHuman):
                record(
                    Step(index=len(state.steps), kind=StepKind.final, summary=decision.question[:300])
                )
                return self._finish(state, RunStatus.completed, decision.question, meter)

            if not isinstance(decision, ToolCall):  # defensive: unknown planner output
                return self._finish(state, RunStatus.failed, "The planner returned an unsupported decision.")

            state.usage.steps += 1
            record(
                Step(
                    index=len(state.steps),
                    kind=StepKind.tool,
                    summary=f"call {decision.name}",
                    tool=decision.name,
                    args=decision.args,
                )
            )

            try:
                result = self.registry.call(decision.name, decision.args, ctx)
            except ApprovalRequired as exc:
                approval = self.store.get_approval(exc.approval_id) or {}
                state.approval_id = exc.approval_id
                record(
                    Step(
                        index=len(state.steps),
                        kind=StepKind.approval,
                        summary=f"{decision.name} paused for human approval",
                        tool=decision.name,
                        ok=None,
                    )
                )
                answer = (
                    f"`{decision.name}` changes a record, so it needs a second person to approve it.\n"
                    f"Approval `{exc.approval_id}` is pending. Approve or deny it, then resume run `{state.id}`."
                )
                return self._finish(
                    state,
                    RunStatus.awaiting_approval,
                    answer,
                    meter,
                    approval=_to_approval(approval) if approval else None,
                )
            except AgentError as exc:
                # Recoverable: tell the planner what went wrong and let it adapt once.
                state.messages.append({"role": "tool", "content": observation(decision.name, False, exc.to_dict())})
                record(
                    Step(
                        index=len(state.steps),
                        kind=StepKind.observation,
                        summary=f"{decision.name} failed: {exc.message}",
                        tool=decision.name,
                        ok=False,
                    )
                )
                state.usage.steps += 1
                continue

            state.usage.tool_calls += 1
            state.messages.append({"role": "tool", "content": observation(decision.name, True, result.data)})
            record(
                Step(
                    index=len(state.steps),
                    kind=StepKind.observation,
                    summary=f"{decision.name} returned in {result.latency_ms}ms",
                    tool=decision.name,
                    ok=True,
                    latency_ms=result.latency_ms,
                )
            )
            state.usage.steps += 1

        return self._finish(
            state,
            RunStatus.budget_exceeded,
            f"Stopped after {state.usage.steps} steps without a final answer. Ask a narrower question.",
            meter,
        )

    # -- helpers -----------------------------------------------------------
    def _finish(
        self,
        state: _RunState,
        status: RunStatus,
        answer: str,
        meter: UsageMeter | None = None,
        approval: ApprovalRecord | None = None,
    ) -> AgentRunResponse:
        if meter is not None:
            state.usage.prompt_tokens = meter.prompt_tokens
            state.usage.completion_tokens = meter.completion_tokens
            state.usage.estimated_cost_usd = meter.cost_usd()
        return AgentRunResponse(
            run_id=state.id,
            status=status,
            answer=answer,
            steps=state.steps,
            usage=state.usage,
            approval=approval,
        )

    def _persist(self, state: _RunState, principal: Principal, response: AgentRunResponse) -> None:
        self.store.save_run(
            {
                "id": state.id,
                "session_id": state.session_id,
                "task": state.task,
                "purpose": state.purpose,
                "principal_sub": principal.sub,
                "role": principal.role.value,
                "tenant": principal.tenant,
                "status": response.status.value,
                "messages": state.messages,
                "steps": [s.model_dump(mode="json") for s in state.steps],
                "usage": state.usage.model_dump(mode="json"),
                "answer": response.answer,
                "approval_id": state.approval_id,
                "created_at": utcnow(),
            }
        )
        self.audit.record(
            principal,
            action="agent.run.finish",
            outcome=response.status.value,
            detail={
                "run_id": state.id,
                "steps": state.usage.steps,
                "tool_calls": state.usage.tool_calls,
                "tokens": state.usage.prompt_tokens + state.usage.completion_tokens,
            },
        )
        log.info(
            "agent run finished",
            extra={"run_id": state.id, "status": response.status.value, "steps": state.usage.steps},
        )


def _to_approval(record: dict[str, Any]) -> ApprovalRecord:
    return ApprovalRecord(
        id=record["id"],
        run_id=record["run_id"],
        tool=record["tool"],
        args=record["args"],
        requested_by=record["requested_by"],
        status=record["status"],
        created_at=record["created_at"],
        expires_at=record["expires_at"],
        decided_by=record.get("decided_by"),
        reason=record.get("reason"),
    )
