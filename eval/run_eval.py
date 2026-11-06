#!/usr/bin/env python
"""Deterministic evaluation harness.

Runs every case against the offline planner so it can gate CI without an API key.
The same file doubles as a regression suite for prompt/routing changes: a case
records the tool that should be chosen, the facts that must appear in the answer,
and (for write actions) the terminal status.

    .venv/bin/python eval/run_eval.py            # human table
    .venv/bin/python eval/run_eval.py --json     # machine output
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from hospital_agent.config import Settings  # noqa: E402
from hospital_agent.container import Container  # noqa: E402
from hospital_agent.models import AgentRunRequest, Principal, Role  # noqa: E402
from hospital_agent.store import Store  # noqa: E402

CASES = Path(__file__).with_name("cases.json")
PURPOSES = ("operations", "treatment", "payment")


def run_case(case: dict) -> dict:
    container = Container.build(
        Settings(db_path=":memory:", llm_provider="deterministic"),
        store=Store(":memory:"),
        configure_logs=False,
    )
    principal = Principal(sub=f"eval-{case['role']}", role=Role(case["role"]), tenant="eval", purposes=PURPOSES)
    response = container.agent.run(
        AgentRunRequest(task=case["task"], purpose_of_use=case.get("purpose", "operations")), principal
    )
    tools = [step.tool for step in response.steps if step.tool]
    text = response.answer or ""

    failures: list[str] = []
    expected_tool = case.get("expect_tool")
    if expected_tool and expected_tool not in tools:
        failures.append(f"expected tool {expected_tool}, used {tools or 'none'}")
    if case.get("expect_not_tool") and case["expect_not_tool"] in tools:
        failures.append(f"used forbidden tool {case['expect_not_tool']}")
    for needle in case.get("expect_contains", []):
        if needle.lower() not in text.lower():
            failures.append(f"missing '{needle}'")
    if case.get("expect_status") and response.status.value != case["expect_status"]:
        failures.append(f"status {response.status.value} != {case['expect_status']}")

    return {
        "id": case["id"],
        "passed": not failures,
        "failures": failures,
        "tool": tools[0] if tools else None,
        "status": response.status.value,
        "answer": text,
        "steps": len(response.steps),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--report", default=str(Path(__file__).with_name("latest-report.md")))
    args = parser.parse_args()

    cases = json.loads(CASES.read_text())
    results = [run_case(case) for case in cases]
    passed = sum(1 for r in results if r["passed"])

    if args.json:
        print(json.dumps({"passed": passed, "total": len(results), "results": results}, indent=2))
    else:
        print(f"\n{'CASE':<24} {'RESULT':<7} {'TOOL':<26} STATUS")
        print("-" * 78)
        for r in results:
            print(f"{r['id']:<24} {'PASS' if r['passed'] else 'FAIL':<7} {str(r['tool']):<26} {r['status']}")
            for failure in r["failures"]:
                print(f"    - {failure}")
        print("-" * 78)
        print(f"{passed}/{len(results)} cases passed\n")

    report = ["# Evaluation report", "", f"**{passed}/{len(results)} cases passed**", "", "| case | result | tool | status |", "|---|---|---|---|"]
    for r in results:
        report.append(f"| {r['id']} | {'pass' if r['passed'] else 'fail'} | `{r['tool']}` | {r['status']} |")
    if passed != len(results):
        report += ["", "## Failures", ""]
        for r in results:
            if not r["passed"]:
                report.append(f"- **{r['id']}**: " + "; ".join(r["failures"]))
    Path(args.report).write_text("\n".join(report) + "\n")

    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
