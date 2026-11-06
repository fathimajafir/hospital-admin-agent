"""Environment-driven configuration.

Everything is resolved once at process start and frozen. Nothing in the request
path reads ``os.environ`` directly, which keeps behaviour reproducible and makes
tests able to inject a Settings object.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from pydantic import ValidationError

from .models import Principal, Role


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    env: str = field(default_factory=lambda: os.getenv("HA_ENV", "dev"))
    log_level: str = field(default_factory=lambda: os.getenv("HA_LOG_LEVEL", "INFO").upper())

    db_path: str = field(default_factory=lambda: os.getenv("HA_DB_PATH", "./var/hospital.db"))
    policy_path: str | None = field(default_factory=lambda: os.getenv("HA_POLICY_PATH") or None)

    llm_provider: str = field(default_factory=lambda: os.getenv("HA_LLM_PROVIDER", "deterministic").lower())
    llm_base_url: str = field(default_factory=lambda: os.getenv("HA_LLM_BASE_URL", "https://api.openai.com/v1"))
    llm_api_key: str = field(default_factory=lambda: os.getenv("HA_LLM_API_KEY", ""))
    llm_model: str = field(default_factory=lambda: os.getenv("HA_LLM_MODEL", "gpt-4o-mini"))
    llm_timeout_s: float = field(default_factory=lambda: _env_float("HA_LLM_TIMEOUT_S", 30.0))

    max_steps: int = field(default_factory=lambda: _env_int("HA_MAX_STEPS", 8))
    token_budget: int = field(default_factory=lambda: _env_int("HA_TOKEN_BUDGET", 12_000))
    run_timeout_s: float = field(default_factory=lambda: _env_float("HA_RUN_TIMEOUT_S", 45.0))
    approval_ttl_s: int = field(default_factory=lambda: _env_int("HA_APPROVAL_TTL_S", 1800))
    require_phi_purpose: bool = field(default_factory=lambda: _env_bool("HA_REQUIRE_PHI_PURPOSE", True))
    max_body_bytes: int = field(default_factory=lambda: _env_int("HA_MAX_BODY_BYTES", 1_048_576))
    rate_limit_rpm: int = field(default_factory=lambda: _env_int("HA_RATE_LIMIT_RPM", 240))

    api_keys_raw: str = field(default_factory=lambda: os.getenv("HA_API_KEYS", ""))

    def principals(self) -> dict[str, Principal]:
        """Parse ``HA_API_KEYS`` into a key -> Principal map.

        A malformed entry is a startup error, not a silent downgrade: an agent
        that silently loses its auth config is worse than one that refuses to boot.
        """
        raw = self.api_keys_raw.strip()
        if not raw:
            return {
                "dev-admin-key": Principal(
                    sub="local-admin",
                    role=Role.admin,
                    tenant="demo",
                    purposes=("treatment", "operations", "payment", "research"),
                )
            }
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:  # pragma: no cover - startup guard
            raise SystemExit(f"HA_API_KEYS is not valid JSON: {exc}") from exc

        out: dict[str, Principal] = {}
        for key, body in parsed.items():
            try:
                out[key] = Principal(
                    sub=body.get("sub", key[:8]),
                    role=Role(body.get("role", "service")),
                    tenant=body.get("tenant", "default"),
                    purposes=tuple(body.get("purposes", ())),
                )
            except (ValidationError, ValueError) as exc:  # pragma: no cover - startup guard
                raise SystemExit(f"HA_API_KEYS entry {key[:6]}... is invalid: {exc}") from exc
        return out


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def ensure_parent_dir(path: str) -> None:
    if path in {":memory:", ""}:
        return
    Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
