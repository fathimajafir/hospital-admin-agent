"""Tamper-evident audit log.

Every tool invocation, policy denial and approval becomes one row whose hash
covers the previous row's hash. Editing or deleting a row breaks ``verify()``,
which is what makes the log usable as evidence rather than a debug trail.

What is stored is a *hash* of the arguments, never the arguments themselves:
the audit table must not become a second, unmanaged copy of PHI.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .models import Principal, utcnow
from .redact import redact_text

GENESIS = "0" * 64


def canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def digest(prev_hash: str, payload: dict[str, Any]) -> str:
    return hashlib.sha256((prev_hash + canonical(payload)).encode("utf-8")).hexdigest()


def args_fingerprint(args: dict[str, Any]) -> str:
    """Stable, non-reversible fingerprint of a tool's arguments."""
    return hashlib.sha256(canonical(args).encode("utf-8")).hexdigest()[:32]


class AuditLog:
    def __init__(self, store: Any) -> None:
        self._store = store

    def record(
        self,
        principal: Principal,
        *,
        action: str,
        outcome: str,
        tool: str | None = None,
        args: dict[str, Any] | None = None,
        detail: dict[str, Any] | None = None,
    ) -> int:
        prev = self._store.last_audit_hash() or GENESIS
        payload = {
            "ts": utcnow(),
            "actor": principal.sub,
            "role": principal.role.value,
            "tenant": principal.tenant,
            "action": action,
            "tool": tool,
            "outcome": outcome,
            "args_hash": args_fingerprint(args) if args else None,
            "detail": _safe_detail(detail or {}),
        }
        return self._store.insert_audit(payload, prev_hash=prev, hash_value=digest(prev, payload))

    def verify(self) -> tuple[bool, str]:
        prev = GENESIS
        for row in self._store.list_audit(limit=0):
            payload = {
                "ts": row["ts"],
                "actor": row["actor"],
                "role": row["role"],
                "tenant": row["tenant"],
                "action": row["action"],
                "tool": row["tool"],
                "outcome": row["outcome"],
                "args_hash": row["args_hash"],
                "detail": row["detail"],
            }
            if row["prev_hash"] != prev:
                return False, f"chain break at audit id {row['id']}: prev_hash mismatch"
            if digest(prev, payload) != row["hash"]:
                return False, f"chain break at audit id {row['id']}: hash mismatch"
            prev = row["hash"]
        return True, "ok"


def _safe_detail(detail: dict[str, Any]) -> dict[str, Any]:
    """Keep small scalars, drop anything that looks like PHI or a payload."""
    safe: dict[str, Any] = {}
    for key, value in detail.items():
        if key.lower() in {"name", "dob", "phone", "email", "mrn", "address"}:
            safe[key] = "[redacted]"
        elif isinstance(value, (str, int, float, bool)) or value is None:
            safe[key] = redact_text(value) if isinstance(value, str) else value
        elif isinstance(value, (list, tuple)) and all(isinstance(v, (str, int, float, bool)) for v in value):
            safe[key] = [redact_text(v) if isinstance(v, str) else v for v in value][:20]
        else:
            safe[key] = "[omitted]"
    return safe
