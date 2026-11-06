"""Model-agnostic planning interface.

The agent does not depend on any provider SDK. A planner receives the task, the
conversation so far, and the tool catalog, and returns exactly one decision.

Using a JSON decision protocol (rather than provider-specific tool-call objects)
means the same agent works against OpenAI, Bedrock, Ollama or a mock in a test.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


@dataclass
class ToolCall:
    name: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass
class Final:
    text: str


@dataclass
class AskHuman:
    question: str


Decision = ToolCall | Final | AskHuman


class Planner(Protocol):
    name: str

    def plan(
        self,
        *,
        task: str,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]],
        remaining_steps: int,
    ) -> Decision: ...


class UsageMeter:
    """Tracks token and cost estimates across a run."""

    def __init__(self) -> None:
        self.prompt_tokens = 0
        self.completion_tokens = 0

    def add(self, prompt_tokens: int, completion_tokens: int) -> None:
        self.prompt_tokens += max(0, prompt_tokens)
        self.completion_tokens += max(0, completion_tokens)

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def cost_usd(self, prompt_per_1k: float = 0.0005, completion_per_1k: float = 0.0015) -> float:
        return round(
            (self.prompt_tokens / 1000) * prompt_per_1k + (self.completion_tokens / 1000) * completion_per_1k,
            6,
        )


def estimate_tokens(text: str) -> int:
    """Cheap upper-ish bound used for budgeting when the provider reports nothing."""
    return max(1, len(text) // 4)


Level = Literal["info", "warn", "error"]
