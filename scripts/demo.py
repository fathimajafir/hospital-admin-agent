#!/usr/bin/env python
"""Three tasks that show the interesting parts of the agent.

    .venv/bin/python scripts/demo.py
"""

from __future__ import annotations

from hospital_agent.config import Settings
from hospital_agent.container import Container
from hospital_agent.models import AgentRunRequest, Principal, Role
from hospital_agent.store import Store

TASKS = [
    ("morning command centre", "give me the morning ops digest", "operations"),
    ("discharge blockers", "which discharges are blocked today?", "operations"),
    ("prior authorisation", "build a prior auth packet for P0003 for 27447", "payment"),
]


def main() -> None:
    container = Container.build(Settings(db_path=":memory:"), store=Store(":memory:"), configure_logs=False)
    principal = Principal(
        sub="demo-admin", role=Role.admin, tenant="demo", purposes=("operations", "treatment", "payment")
    )

    for title, task, purpose in TASKS:
        response = container.agent.run(
            AgentRunRequest(task=task, purpose_of_use=purpose), principal
        )
        print("=" * 78)
        print(f"{title.upper()}  ->  {task}")
        print("=" * 78)
        print(response.answer)
        print()

    print("=" * 78)
    print("AUDIT")
    print("=" * 78)
    ok, detail = container.audit.verify()
    print(f"hash chain intact: {ok} ({detail})")
    print(f"entries: {len(container.store.list_audit(limit=0))}")


if __name__ == "__main__":
    main()
