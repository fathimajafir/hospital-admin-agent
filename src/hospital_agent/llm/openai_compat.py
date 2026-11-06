"""Any OpenAI-compatible ``/chat/completions`` endpoint as the planner.

Works unchanged against OpenAI, Azure OpenAI, vLLM, Ollama, LiteLLM, or a
Bedrock gateway - the only requirement is that the endpoint honours
``response_format={"type": "json_object"}``.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from ..agent.prompts import build_system_prompt
from ..errors import UpstreamError
from ..logging_setup import get_logger
from .base import AskHuman, Decision, Final, ToolCall, UsageMeter, estimate_tokens

log = get_logger("hospital_agent.llm")


class OpenAICompatiblePlanner:
    name = "openai-compatible"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_s: float = 30.0,
        meter: UsageMeter | None = None,
        temperature: float = 0.1,
    ) -> None:
        if not api_key:
            raise UpstreamError("HA_LLM_API_KEY is not set for provider 'openai'")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_s = timeout_s
        self.meter = meter or UsageMeter()
        self.temperature = temperature

    def plan(
        self,
        *,
        task: str,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]],
        remaining_steps: int,
    ) -> Decision:
        system = build_system_prompt(tools)
        system += f"\n\nSteps remaining: {remaining_steps}. Prefer answering over another tool call."
        payload = {
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": 900,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system}, *messages],
        }

        data = self._post(payload)
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as exc:
            raise UpstreamError("malformed completion from provider") from exc

        usage = data.get("usage") or {}
        self.meter.add(
            int(usage.get("prompt_tokens") or estimate_tokens(system + task)),
            int(usage.get("completion_tokens") or estimate_tokens(content or "")),
        )

        return parse_decision(content or "")

    # -- transport ---------------------------------------------------------
    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                with httpx.Client(timeout=self.timeout_s) as client:
                    response = client.post(f"{self.base_url}/chat/completions", json=payload, headers=headers)
                if response.status_code in {429, 500, 502, 503, 504}:
                    raise UpstreamError(f"provider returned {response.status_code}")
                if response.status_code >= 400:
                    raise UpstreamError(
                        f"provider rejected the request ({response.status_code})",
                        details={"body": response.text[:500]},
                    )
                return response.json()
            except (httpx.HTTPError, UpstreamError) as exc:
                last_error = exc
                if attempt == 0:
                    time.sleep(0.5)
                    continue
        raise UpstreamError(f"planner unavailable: {last_error}")


def parse_decision(content: str) -> Decision:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    try:
        body = json.loads(text)
    except json.JSONDecodeError as exc:
        raise UpstreamError("planner did not return valid JSON", details={"preview": content[:300]}) from exc

    if not isinstance(body, dict):
        raise UpstreamError("planner returned a non-object decision")

    if "final" in body:
        return Final(str(body["final"]))
    if "question" in body:
        return AskHuman(str(body["question"]))
    if "tool" in body:
        args = body.get("args") or {}
        if not isinstance(args, dict):
            raise UpstreamError("planner returned non-object args")
        return ToolCall(str(body["tool"]), args)
    raise UpstreamError("planner returned an unrecognised decision", details={"keys": sorted(body)})
