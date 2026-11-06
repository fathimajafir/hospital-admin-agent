"""Command line entry point.

    ha-agent tools                      list tools for the demo admin role
    ha-agent ask "morning digest"       one agent run, printed as a report
    ha-agent call capacity_snapshot     one tool call
    ha-agent audit verify               check the hash chain
    ha-agent serve --port 8080          run the HTTP API
    ha-agent seed --reset               rebuild the synthetic dataset
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .config import Settings
from .container import Container
from .models import AgentRunRequest, Principal, Role, ToolCallRequest
from .store import Store
from .tools.registry import ToolContext


def _principal(role: str, purposes: str) -> Principal:
    return Principal(
        sub=f"cli-{role}",
        role=Role(role),
        tenant="cli",
        purposes=tuple(p.strip() for p in purposes.split(",") if p.strip()),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ha-agent", description="Hospital administration agent")
    parser.add_argument("--role", default="admin", help="role to act as (default: admin)")
    parser.add_argument("--purpose", default="operations,treatment,payment", help="comma-separated purposes")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("tools", help="list available tools")
    sub.add_parser("audit", help="verify the audit hash chain")

    ask = sub.add_parser("ask", help="run one agent task")
    ask.add_argument("task")
    ask.add_argument("--json", action="store_true", help="print the raw response")

    call = sub.add_parser("call", help="call one tool directly")
    call.add_argument("tool")
    call.add_argument("args", nargs="?", default="{}", help="JSON object of arguments")

    serve = sub.add_parser("serve", help="run the HTTP API")
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument("--reload", action="store_true")

    seed = sub.add_parser("seed", help="seed the synthetic dataset")
    seed.add_argument("--reset", action="store_true")

    args = parser.parse_args(argv)
    settings = Settings()
    container = Container.build(settings, log_stream=sys.stderr)
    principal = _principal(args.role, args.purpose)

    if args.command == "tools":
        for tool in container.registry.catalog(principal):
            flag = " [approval]" if tool["requires_approval"] else ""
            print(f"{tool['name']}{flag}\n    {tool['description']}\n")
        return 0

    if args.command == "audit":
        ok, detail = container.audit.verify()
        print(json.dumps({"intact": ok, "detail": detail}, indent=2))
        return 0 if ok else 1

    if args.command == "ask":
        response = container.agent.run(
            AgentRunRequest(task=args.task, purpose_of_use=args.purpose.split(",")[0]), principal
        )
        if args.json:
            print(response.model_dump_json(indent=2))
        else:
            print(f"\n[{response.status.value}] {response.answer}\n")
            print(f"steps={response.usage.steps} tool_calls={response.usage.tool_calls} "
                  f"tokens={response.usage.prompt_tokens + response.usage.completion_tokens}")
            for step in response.steps:
                marker = "ok" if step.ok else ("--" if step.ok is None else "!!")
                print(f"  {step.index:>2}. [{marker}] {step.kind.value:<11} {step.summary[:110]}")
        return 0 if response.status.value in {"completed", "awaiting_approval"} else 1

    if args.command == "call":
        try:
            payload = json.loads(args.args)
        except json.JSONDecodeError as exc:
            print(f"args must be JSON: {exc}", file=sys.stderr)
            return 2
        ctx = ToolContext(
            store=container.store,
            principal=principal,
            purpose=args.purpose.split(",")[0],
            settings=settings,
        )
        result = container.registry.call(args.tool, payload, ctx)
        print(json.dumps(result.model_dump(mode="json"), indent=2))
        return 0

    if args.command == "seed":
        container.store.seed(reset=args.reset)
        print(json.dumps({"seeded": True, "beds": len(container.store.beds())}))
        return 0

    if args.command == "serve":
        import uvicorn

        uvicorn.run("hospital_agent.api.app:app", host=args.host, port=args.port, reload=args.reload)
        return 0

    return 1


def _demo() -> None:  # pragma: no cover - convenience for `python -m hospital_agent.cli`
    raise SystemExit(main())


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
