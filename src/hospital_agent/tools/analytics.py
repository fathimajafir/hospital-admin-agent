"""Operations digest and quality indicators.

The digest is the "single screen" the agent produces: what is on fire, what it
costs, and the three moves with the highest expected payoff today.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ..models import utcnow
from .capacity import CapacityArgs, DischargeArgs, StaffingArgs, capacity_snapshot, discharge_board, staffing_gaps
from .registry import ToolContext, ToolSpec
from .scoring import no_show_probability

BENCHMARKS = {
    "occupancy": 0.85,
    "projected_no_show_rate": 0.12,
    "denial_risk_share": 0.10,
    "boarders": 2,
}


class DigestArgs(BaseModel):
    ward: str | None = Field(None, description="Focus on one ward. Omit for the whole hospital.")
    include_financial: bool = Field(True, description="Include revenue-cycle actions.")


def ops_digest(ctx: ToolContext, args: DigestArgs) -> dict[str, Any]:
    capacity = capacity_snapshot(ctx, CapacityArgs(ward=args.ward))
    discharges = discharge_board(ctx, DischargeArgs(ward=args.ward, limit=10))
    staffing = staffing_gaps(ctx, StaffingArgs(ward=args.ward))

    actions: list[dict[str, Any]] = []

    ready = discharges["ready_today"]
    if ready:
        actions.append(
            {
                "priority": 1,
                "domain": "flow",
                "action": f"discharge the {ready} patient(s) already at readiness >= 0.85 today",
                "impact": f"frees ~{ready} bed(s) before the afternoon admission wave",
                "owner": "charge nurse + case management",
                "rationale": "discharge before noon is the cheapest way to create capacity",
                "score": 0.9 * min(ready, 4) / 4,
            }
        )

    critical = [s for s in staffing["shifts"] if s["severity"] == "critical"]
    if critical:
        worst = critical[0]
        actions.append(
            {
                "priority": 2,
                "domain": "workforce",
                "action": worst["action"],
                "impact": f"{len(critical)} shift(s) below the acuity-adjusted requirement",
                "owner": "nurse staffing office",
                "rationale": f"{worst['ward']} {worst['shift']} has the largest gap",
                "score": 0.8 * min(len(critical), 4) / 4,
            }
        )

    if capacity["pressure"]["band"] in {"high", "critical"}:
        top = capacity["wards"][0]
        actions.append(
            {
                "priority": 3,
                "domain": "capacity",
                "action": top.get("action", "activate surge protocol"),
                "impact": f"hospital pressure {capacity['pressure']['pressure']:.2f} ({capacity['pressure']['band']})",
                "owner": "house supervisor",
                "rationale": f"{top['ward']} is the tightest unit at {int(top['occupancy'] * 100)}% occupancy",
                "score": 0.7,
            }
        )

    revenue = None
    if args.include_financial:
        from .billing import SummaryArgs, billing_summary

        try:
            revenue = billing_summary(ctx, SummaryArgs(limit=5))
        except ValueError:
            revenue = None
        if revenue and revenue["at_risk_usd"] > 0:
            actions.append(
                {
                    "priority": 4,
                    "domain": "revenue",
                    "action": f"fix the {len(revenue['priority_claims'])} highest-risk claims before submission",
                    "impact": f"${revenue['at_risk_usd']:,.0f} of charges currently exposed",
                    "owner": "revenue cycle",
                    "rationale": "preventable denials convert directly to collected revenue",
                    "score": 0.6 * min(revenue["at_risk_share"], 1.0),
                }
            )

    actions.sort(key=lambda a: -a["score"])
    for rank, action in enumerate(actions, start=1):
        action["priority"] = rank

    return {
        "generated_at": utcnow(),
        "scope": args.ward or "hospital-wide",
        "headline": _headline(capacity, discharges, staffing),
        "pressure": capacity["pressure"],
        "key_numbers": {
            "occupancy": capacity["totals"]["occupancy"],
            "boarders": capacity["totals"]["boarders"],
            "ready_today": ready,
            "at_risk_discharges": discharges["at_risk"],
            "critical_shifts": staffing["critical_shifts"],
            "at_risk_usd": round(revenue["at_risk_usd"], 2) if revenue else None,
        },
        "top_actions": actions[:5],
    }


def _headline(capacity: dict[str, Any], discharges: dict[str, Any], staffing: dict[str, Any]) -> str:
    band = capacity["pressure"]["band"]
    ready = _plural(discharges["ready_today"], "discharge")
    shifts = _plural(staffing["critical_shifts"], "short-staffed shift")
    occupancy = int(capacity["totals"]["occupancy"] * 100)
    if band == "critical":
        return (
            f"Critical pressure ({capacity['pressure']['pressure']:.2f}): "
            f"{_plural(capacity['totals']['boarders'], 'boarder')}, {ready} ready now, {shifts}."
        )
    if band == "high":
        return f"High pressure: occupancy {occupancy}%, {ready} ready, {shifts}."
    return f"Stable operations: occupancy {occupancy}%, no escalation needed."


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" + ("" if count == 1 else "s")


class QualityArgs(BaseModel):
    limit: int = Field(200, ge=10, le=1000, description="Maximum records to sample.")


def quality_indicators(ctx: ToolContext, args: QualityArgs) -> dict[str, Any]:
    beds = ctx.store.beds()
    total = len(beds)
    occupied = sum(1 for b in beds if b["status"] in {"occupied", "boarding"})
    boarders = sum(1 for b in beds if b["status"] == "boarding")

    appts = ctx.store.appointments(limit=args.limit)
    risky = 0
    for appt in appts:
        result = no_show_probability(
            lead_days=appt["lead_days"],
            prior_no_shows=appt["prior_no_shows"],
            appt_type=appt["appt_type"],
            payer="Unknown",
            distance_km=appt["distance_km"],
        )
        if result["probability"] >= 0.35:
            risky += 1
    projected_no_show = round(risky / len(appts), 3) if appts else 0.0

    from .scoring import claim_denial_risk

    claims = ctx.store.claims()
    exposed = sum(1 for c in claims if claim_denial_risk(c)["band"] == "high")
    denial_share = round(exposed / len(claims), 3) if claims else 0.0

    discharges = ctx.store.discharges()
    readiness = [float(len(d["barriers"]) == 0) for d in discharges]
    avg_ready = round(sum(readiness) / len(readiness), 2) if readiness else 0.0

    indicators = [
        _indicator("occupancy", round(occupied / total, 2) if total else 0.0, BENCHMARKS["occupancy"], lower_is_better=False),
        _indicator("ed_boarders", boarders, BENCHMARKS["boarders"], lower_is_better=True),
        _indicator("projected_no_show_rate", projected_no_show, BENCHMARKS["projected_no_show_rate"], lower_is_better=True),
        _indicator("high_denial_risk_share", denial_share, BENCHMARKS["denial_risk_share"], lower_is_better=True),
        {
            "name": "discharge_plans_unblocked",
            "value": avg_ready,
            "unit": "share",
            "benchmark": 0.6,
            "status": "ok" if avg_ready >= 0.6 else "watch",
            "note": "share of planned discharges with no recorded barrier",
        },
    ]

    return {
        "generated_at": utcnow(),
        "sample": {"appointments": len(appts), "claims": len(claims), "discharges": len(discharges)},
        "indicators": indicators,
        "watching": [i["name"] for i in indicators if i["status"] != "ok"],
    }


def _indicator(name: str, value: float, benchmark: float, *, lower_is_better: bool) -> dict[str, Any]:
    if lower_is_better:
        status = "ok" if value <= benchmark else "watch" if value <= benchmark * 1.5 else "breach"
    else:
        status = "ok" if value <= benchmark else "watch" if value <= benchmark * 1.1 else "breach"
    return {
        "name": name,
        "value": value,
        "unit": "ratio",
        "benchmark": benchmark,
        "status": status,
        "direction": "lower_is_better" if lower_is_better else "capacity_ceiling",
    }


def analytics_tools() -> list[ToolSpec]:
    return [
        ToolSpec(
            name="ops_digest",
            description=(
                "The hospital command-centre digest: one headline, the key numbers, and the top "
                "prioritised actions across flow, workforce, capacity and revenue with an owner "
                "and an estimated impact for each."
            ),
            args_model=DigestArgs,
            handler=ops_digest,
            tags=("analytics", "digest"),
        ),
        ToolSpec(
            name="quality_indicators",
            description=(
                "Tracks occupancy, ED boarders, projected no-show rate, denial risk share and "
                "discharge readiness against benchmarks, and lists what is off-target."
            ),
            args_model=QualityArgs,
            handler=quality_indicators,
            tags=("analytics", "quality"),
        ),
    ]
