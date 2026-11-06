"""Authentication and per-key rate limiting."""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any

from fastapi import Header, HTTPException, Request, status

from ..errors import PolicyDenied
from ..models import Principal


def resolve_principal(settings: Any, api_key: str | None) -> Principal:
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "unauthorized", "message": "missing X-API-Key"},
        )
    principals = settings.principals()
    principal = principals.get(api_key)
    if principal is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "unauthorized", "message": "invalid API key"},
        )
    return principal


class RateLimiter:
    """Fixed-window counter per API key.

    In-memory is the right default for a single process. Behind more than one
    replica, move this to Redis - the interface is deliberately tiny.
    """

    def __init__(self, rpm: int) -> None:
        self.rpm = max(1, rpm)
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> tuple[bool, int]:
        now = time.monotonic()
        with self._lock:
            bucket = self._hits.setdefault(key, deque())
            while bucket and now - bucket[0] > 60:
                bucket.popleft()
            if len(bucket) >= self.rpm:
                return False, 0
            bucket.append(now)
            return True, self.rpm - len(bucket)


async def principal_from_request(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
) -> Principal:
    key = x_api_key
    if not key and authorization and authorization.lower().startswith("bearer "):
        key = authorization.split(" ", 1)[1].strip()

    container = request.app.state.container
    principal = resolve_principal(container.settings, key)

    limiter: RateLimiter = request.app.state.limiter
    allowed, remaining = limiter.check(key or "")
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"code": "rate_limited", "message": f"limit is {limiter.rpm} requests/minute"},
        )
    request.state.principal = principal
    request.state.rate_remaining = remaining
    return principal


def require_role(principal: Principal, *allowed: str) -> None:
    """Coarse check for non-tool endpoints such as the audit log."""
    if principal.role.value not in allowed and principal.role.value != "admin":
        raise PolicyDenied(
            f"role '{principal.role.value}' cannot perform this action",
            details={"allowed_roles": list(allowed)},
        )
