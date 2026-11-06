"""Declarative, deny-by-default access control.

Rules live in JSON so that a security reviewer can read them without reading
Python, and so that a change to them is a diff in review - not a code change
buried in a handler.

Precedence, per tool:

1. The role must hold every permission the tool declares.
2. If the tool touches PHI, the caller must supply a recognised purpose of use
   that is also granted to their principal.
3. If the tool is marked ``requires_approval``, a second human must authorise the
   exact arguments before anything happens.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from importlib import resources
from typing import Any

from ..errors import PolicyDenied
from ..models import PURPOSES, Principal

PHI_PERMISSION = "phi"


@dataclass(frozen=True)
class Decision:
    allowed: bool
    requires_approval: bool = False
    reason: str = "ok"
    missing_permissions: tuple[str, ...] = ()
    tool_rule: dict[str, Any] = field(default_factory=dict)


class PolicyEngine:
    def __init__(self, rules: dict[str, Any], *, require_phi_purpose: bool = True) -> None:
        self._roles: dict[str, set[str]] = {
            role: set(perms) for role, perms in rules.get("roles", {}).items()
        }
        self._tools: dict[str, dict[str, Any]] = rules.get("tools", {})
        self._default: dict[str, Any] = rules.get("default", {"permissions": ["admin"]})
        self.require_phi_purpose = require_phi_purpose
        self.version = rules.get("version", 0)

    # -- construction ------------------------------------------------------
    @classmethod
    def from_file(cls, path: str | None = None, *, require_phi_purpose: bool = True) -> "PolicyEngine":
        if path:
            with open(path, "r", encoding="utf-8") as handle:
                return cls(json.load(handle), require_phi_purpose=require_phi_purpose)
        raw = resources.files("hospital_agent.policy").joinpath("rules.default.json").read_text("utf-8")
        return cls(json.loads(raw), require_phi_purpose=require_phi_purpose)

    # -- introspection -----------------------------------------------------
    def rule_for(self, tool: str) -> dict[str, Any]:
        return self._tools.get(tool, self._default)

    def permissions_for_role(self, role: str) -> set[str]:
        perms = set(self._roles.get(role, set()))
        if "*" not in perms:
            return perms
        # "*" expands to every permission any tool declares, so a new tool with a
        # new permission still works for admins without editing the rules file.
        expanded = {"read", "write", "analytics", "schedule", "financial", "clinical", "phi", "admin"}
        for rule in self._tools.values():
            expanded |= set(rule.get("permissions", []))
        return expanded

    def visible_tools(self, principal: Principal, all_tools: list[str]) -> list[str]:
        out = []
        for tool in all_tools:
            decision = self.check(principal, tool, purpose=None, check_purpose=False)
            if decision.allowed:
                out.append(tool)
        return out

    # -- evaluation --------------------------------------------------------
    def check(
        self,
        principal: Principal,
        tool: str,
        *,
        purpose: str | None,
        check_purpose: bool = True,
    ) -> Decision:
        rule = self.rule_for(tool)
        required = set(rule.get("permissions", []))
        held = self.permissions_for_role(principal.role.value)
        missing = tuple(sorted(required - held))

        if missing:
            return Decision(
                allowed=False,
                reason=f"role '{principal.role.value}' lacks permission(s): {', '.join(missing)}",
                missing_permissions=missing,
                tool_rule=rule,
            )

        needs_purpose = bool(rule.get("purpose_required")) or (
            check_purpose and PHI_PERMISSION in required and self.require_phi_purpose
        )
        if check_purpose and needs_purpose:
            if not purpose:
                return Decision(
                    allowed=False,
                    reason="purpose_of_use is required for this tool",
                    tool_rule=rule,
                )
            if purpose not in PURPOSES:
                return Decision(
                    allowed=False,
                    reason=f"unknown purpose_of_use '{purpose}'",
                    tool_rule=rule,
                )
            if not principal.has_purpose(purpose):
                return Decision(
                    allowed=False,
                    reason=f"principal is not granted purpose '{purpose}'",
                    tool_rule=rule,
                )

        return Decision(
            allowed=True,
            requires_approval=bool(rule.get("requires_approval")),
            tool_rule=rule,
        )

    def enforce(self, principal: Principal, tool: str, *, purpose: str | None) -> Decision:
        decision = self.check(principal, tool, purpose=purpose)
        if not decision.allowed:
            raise PolicyDenied(decision.reason, details={"tool": tool, "reason": decision.reason})
        return decision
