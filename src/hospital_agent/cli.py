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

    fhir = sub.add_parser("fhir", help="FHIR R4 ingest and connectivity")
    fhir_sub = fhir.add_subparsers(dest="fhir_command", required=True)

    fhir_sync = fhir_sub.add_parser("sync", help="pull resources into the local store")
    fhir_sync.add_argument(
        "--from",
        dest="source",
        required=True,
        help="FHIR base URL (e.g. https://hapi.fhir.org/baseR4) or a Bundle JSON file",
    )
    fhir_sync.add_argument("--types", default=",".join(("Patient", "Coverage", "Appointment", "Claim")))
    fhir_sync.add_argument("--dry-run", action="store_true", help="report what would change, write nothing")

    fhir_check = fhir_sub.add_parser("check", help="read the server CapabilityStatement")
    fhir_check.add_argument("--base-url", default=None, help="defaults to HA_FHIR_BASE_URL")

    fhir_links = fhir_sub.add_parser("links", help="show FHIR resource to local row mappings")
    fhir_links.add_argument("--limit", type=int, default=50)

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

    if args.command == "fhir":
        return _fhir(args, container, settings)

    if args.command == "serve":
        import uvicorn

        uvicorn.run("hospital_agent.api.app:app", host=args.host, port=args.port, reload=args.reload)
        return 0

    return 1


def _fhir(args: argparse.Namespace, container: Container, settings: Settings) -> int:
    from pathlib import Path

    from .fhir import FhirClient, FhirIngest
    from .fhir.errors import FhirError

    if args.fhir_command == "links":
        print(json.dumps(container.store.fhir_links(args.limit), indent=2))
        return 0

    if args.fhir_command == "check":
        base = args.base_url or settings.fhir_base_url
        if not base:
            print("no FHIR base URL: pass --base-url or set HA_FHIR_BASE_URL", file=sys.stderr)
            return 2
        with FhirClient(base, token=settings.fhir_token or None, timeout_s=settings.fhir_timeout_s) as client:
            capability = client.capability()
        rest = (capability.get("rest") or [{}])[0]
        summary = {
            "source": base,
            "fhir_version": capability.get("fhirVersion"),
            "software": (capability.get("software") or {}).get("name"),
            "resources": sorted({r.get("type") for r in rest.get("resource") or [] if r.get("type")}),
        }
        print(json.dumps(summary, indent=2))
        return 0

    ingest = FhirIngest(container.store, settings, audit=container.audit)
    types = [t.strip() for t in args.types.split(",") if t.strip()]
    source = args.source

    try:
        if source.startswith(("http://", "https://")):
            with FhirClient(
                source,
                token=settings.fhir_token or None,
                timeout_s=settings.fhir_timeout_s,
                page_size=settings.fhir_page_size,
                max_pages=settings.fhir_max_pages,
                verify=settings.fhir_verify_tls,
            ) as client:
                report = ingest.sync(client, types, dry_run=args.dry_run)
        else:
            path = Path(source).expanduser()
            if not path.is_file():
                print(f"not an http(s) URL and not a file: {source}", file=sys.stderr)
                return 2
            report = ingest.apply_bundle(
                json.loads(path.read_text()), source=f"file:{path.name}", dry_run=args.dry_run
            )
    except FhirError as exc:
        print(json.dumps({"error": exc.to_dict()}, indent=2), file=sys.stderr)
        return 1

    print(json.dumps(report.to_dict(), indent=2))
    return 0


def _demo() -> None:  # pragma: no cover - convenience for `python -m hospital_agent.cli`
    raise SystemExit(main())


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
