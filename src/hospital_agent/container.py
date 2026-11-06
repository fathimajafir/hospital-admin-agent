"""Composition root.

Everything is assembled here and nowhere else, so the HTTP app, the MCP server,
the CLI and the tests all share one wiring - and a test can replace exactly one
piece (the store, or the planner) without monkeypatching.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .agent.core import HospitalAgent
from .audit import AuditLog
from .config import Settings, get_settings
from .llm.base import UsageMeter
from .llm.deterministic import DeterministicPlanner
from .llm.openai_compat import OpenAICompatiblePlanner
from .logging_setup import configure_logging
from .policy.engine import PolicyEngine
from .store import Store
from .tools.analytics import analytics_tools
from .tools.billing import billing_tools
from .tools.capacity import capacity_tools
from .tools.patients import patient_tools
from .tools.registry import ToolRegistry
from .tools.scheduling import scheduling_tools


@dataclass
class Container:
    settings: Settings
    store: Store
    policy: PolicyEngine
    audit: AuditLog
    registry: ToolRegistry
    agent: HospitalAgent

    @classmethod
    def build(
        cls,
        settings: Settings | None = None,
        *,
        store: Store | None = None,
        planner_factory: Callable[[UsageMeter], Any] | None = None,
        configure_logs: bool = True,
        log_stream: Any = None,
    ) -> "Container":
        settings = settings or get_settings()
        if configure_logs:
            configure_logging(settings.log_level, stream=log_stream)

        store = store or Store(settings.db_path)
        policy = PolicyEngine.from_file(settings.policy_path, require_phi_purpose=settings.require_phi_purpose)
        audit = AuditLog(store)
        registry = ToolRegistry(policy=policy, audit=audit, settings=settings)
        for group in (
            capacity_tools(),
            scheduling_tools(),
            billing_tools(),
            patient_tools(),
            analytics_tools(),
        ):
            registry.register_all(group)

        factory = planner_factory or _default_planner_factory(settings)
        agent = HospitalAgent(
            store=store,
            registry=registry,
            planner_factory=factory,
            settings=settings,
            audit=audit,
        )
        return cls(
            settings=settings,
            store=store,
            policy=policy,
            audit=audit,
            registry=registry,
            agent=agent,
        )


def _default_planner_factory(settings: Settings) -> Callable[[UsageMeter], Any]:
    def factory(meter: UsageMeter) -> Any:
        if settings.llm_provider in {"openai", "openai-compatible", "bedrock-gateway"}:
            return OpenAICompatiblePlanner(
                base_url=settings.llm_base_url,
                api_key=settings.llm_api_key,
                model=settings.llm_model,
                timeout_s=settings.llm_timeout_s,
                meter=meter,
            )
        return DeterministicPlanner()

    return factory
