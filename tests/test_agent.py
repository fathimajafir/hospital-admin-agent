from __future__ import annotations

from datetime import datetime, timedelta, timezone

from hospital_agent.models import AgentRunRequest, RunStatus


def _future(days: int = 3) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%dT10:00")


def test_digest_run_completes_with_a_useful_answer(container, admin) -> None:
    response = container.agent.run(
        AgentRunRequest(task="give me the morning ops digest", purpose_of_use="operations"), admin
    )
    assert response.status == RunStatus.completed
    assert any(step.tool == "ops_digest" for step in response.steps)
    assert "actions" in response.answer.lower() or "pressure" in response.answer.lower()
    assert response.usage.tool_calls == 1


def test_capacity_question_routes_to_capacity_snapshot(container, admin) -> None:
    response = container.agent.run(
        AgentRunRequest(task="how many beds are free in MED-A?", purpose_of_use="operations"), admin
    )
    assert response.status == RunStatus.completed
    assert response.steps[0].tool == "capacity_snapshot"
    assert "MED-A" in response.answer


def test_agent_never_uses_a_tool_outside_the_catalog(container, analyst) -> None:
    response = container.agent.run(
        AgentRunRequest(task="show me patient P0005", purpose_of_use="operations"), analyst
    )
    assert response.status == RunStatus.completed
    used = {step.tool for step in response.steps if step.tool}
    assert "patient_get" not in used


def test_write_action_pauses_for_approval_then_resumes(container, admin) -> None:
    task = f"book P0007 with Cardiology at {_future()}"
    response = container.agent.run(AgentRunRequest(task=task, purpose_of_use="operations"), admin)

    assert response.status == RunStatus.awaiting_approval
    assert response.approval is not None
    assert response.approval.tool == "schedule_appointment"
    assert response.approval.status == "pending"

    container.store.decide_approval(
        response.approval.id, decision="approved", decided_by="u-admin2", reason="verified with patient"
    )
    resumed = container.agent.resume(response.run_id, admin, response.approval.id)

    assert resumed.status == RunStatus.completed
    assert "booked" in resumed.answer.lower()
    assert resumed.run_id == response.run_id

    stored = container.store.get_run(response.run_id)
    assert stored is not None
    assert stored["status"] == "completed"


def test_resume_refuses_another_users_run(container, admin) -> None:
    from hospital_agent.models import Principal, Role

    task = f"book P0007 with Cardiology at {_future()}"
    response = container.agent.run(AgentRunRequest(task=task, purpose_of_use="operations"), admin)
    other = Principal(sub="someone-else", role=Role.admin, purposes=("operations",))

    try:
        container.agent.resume(response.run_id, other, response.approval.id or "")
        raise AssertionError("expected refusal")
    except Exception as exc:  # AgentError
        assert "not found" in str(exc)


def test_run_is_persisted_and_queryable(container, admin) -> None:
    response = container.agent.run(
        AgentRunRequest(task="quality indicators please", purpose_of_use="operations"), admin
    )
    stored = container.store.get_run(response.run_id)
    assert stored["task"] == "quality indicators please"
    assert stored["messages"]
    assert stored["steps"]


def test_step_callback_streams_updates(container, admin) -> None:
    seen = []
    container.agent.run(
        AgentRunRequest(task="capacity snapshot", purpose_of_use="operations"),
        admin,
        on_step=lambda step: seen.append(step.kind.value),
    )
    assert "tool" in seen
    assert "observation" in seen
    assert "final" in seen
