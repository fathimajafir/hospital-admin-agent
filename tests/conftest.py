from __future__ import annotations

import json

import pytest

from hospital_agent.api.app import create_app
from hospital_agent.config import Settings
from hospital_agent.container import Container
from hospital_agent.models import Principal, Role
from hospital_agent.store import Store

API_KEYS = {
    "admin-key": {"sub": "u-admin", "role": "admin", "tenant": "t1", "purposes": ["treatment", "operations", "payment"]},
    "admin2-key": {"sub": "u-admin2", "role": "admin", "tenant": "t1", "purposes": ["treatment", "operations"]},
    "analyst-key": {"sub": "u-analyst", "role": "analyst", "tenant": "t1", "purposes": ["operations"]},
    "clinician-key": {"sub": "u-clin", "role": "clinician", "tenant": "t1", "purposes": ["treatment"]},
    "billing-key": {"sub": "u-bill", "role": "billing", "tenant": "t1", "purposes": ["payment"]},
}


@pytest.fixture()
def settings() -> Settings:
    return Settings(
        db_path=":memory:",
        api_keys_raw=json.dumps(API_KEYS),
        llm_provider="deterministic",
        rate_limit_rpm=10_000,
        approval_ttl_s=600,
    )


@pytest.fixture()
def container(settings: Settings) -> Container:
    return Container.build(settings, store=Store(":memory:"), configure_logs=False)


@pytest.fixture()
def admin() -> Principal:
    return Principal(sub="u-admin", role=Role.admin, tenant="t1", purposes=("treatment", "operations", "payment"))


@pytest.fixture()
def analyst() -> Principal:
    return Principal(sub="u-analyst", role=Role.analyst, tenant="t1", purposes=("operations",))


@pytest.fixture()
def clinician() -> Principal:
    return Principal(sub="u-clin", role=Role.clinician, tenant="t1", purposes=("treatment",))


@pytest.fixture()
def client(container: Container):
    from fastapi.testclient import TestClient

    return TestClient(create_app(container))


@pytest.fixture()
def admin_headers() -> dict[str, str]:
    return {"X-API-Key": "admin-key"}
