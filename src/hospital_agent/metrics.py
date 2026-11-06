"""Minimal Prometheus text exposition.

Deliberately dependency-free: counters and gauges are enough to answer "is it
up, how slow, and what is failing" without pulling in a client library.
"""

from __future__ import annotations

import threading
from collections import defaultdict

_LOCK = threading.Lock()
_COUNTERS: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
_GAUGES: dict[str, float] = {}


def inc(name: str, value: float = 1.0, **labels: str) -> None:
    key = (name, tuple(sorted(labels.items())))
    with _LOCK:
        _COUNTERS[key] += value


def gauge(name: str, value: float) -> None:
    with _LOCK:
        _GAUGES[name] = value


def render() -> str:
    lines: list[str] = []
    with _LOCK:
        for (name, labels), value in sorted(_COUNTERS.items()):
            if labels:
                label_str = ",".join(f'{k}="{v}"' for k, v in labels)
                lines.append(f"{name}{{{label_str}}} {value:g}")
            else:
                lines.append(f"{name} {value:g}")
        for name, value in sorted(_GAUGES.items()):
            lines.append(f"{name} {value:g}")
    return "\n".join(lines) + "\n"


def reset() -> None:
    with _LOCK:
        _COUNTERS.clear()
        _GAUGES.clear()
