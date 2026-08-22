"""Deterministic five-way capability enforcement and the tool gateway registry.

PRD §13: effective capabilities = user ∩ agent ∩ skill ∩ channel ∩ workspace.
PRD §14: every tool request passes permission check, policy check, approval if
required, execution, and an audit event; unauthorized tools are unavailable or
rejected. Everything here is a pure function of durable records.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

CAPABILITIES: frozenset[str] = frozenset(
    {
        "analysis",
        "coding",
        "decision_making",
        "planning",
        "research",
        "retrieval",
        "review",
        "security",
        "synthesis",
        "testing",
        "writing",
    }
)
MUTATING_CAPABILITIES: frozenset[str] = frozenset({"coding", "testing", "writing"})

_ROLE_CAPABILITIES: dict[str, frozenset[str]] = {
    "admin": CAPABILITIES,
    "editor": CAPABILITIES,
    # Existing pre-auth records used "member"; it is equivalent to editor.
    "member": CAPABILITIES,
    "viewer": CAPABILITIES - MUTATING_CAPABILITIES,
}


def user_capabilities(room_role: str | None) -> frozenset[str]:
    """Capabilities a human may lend to an agent, from durable room membership only."""
    return _ROLE_CAPABILITIES.get(room_role or "", frozenset())


def policy_capabilities(allowed: Iterable[str] | None) -> frozenset[str]:
    """A channel or workspace policy. A policy never set allows the full vocabulary."""
    if allowed is None:
        return CAPABILITIES
    return frozenset(allowed) & CAPABILITIES


@dataclass(frozen=True, slots=True)
class CapabilityTerms:
    user: frozenset[str]
    agent: frozenset[str]
    skill: frozenset[str]
    channel: frozenset[str]
    workspace: frozenset[str]

    @property
    def effective(self) -> frozenset[str]:
        return self.user & self.agent & self.skill & self.channel & self.workspace

    def as_dict(self) -> dict[str, list[str]]:
        return {
            "user": sorted(self.user),
            "agent": sorted(self.agent),
            "skill": sorted(self.skill),
            "channel": sorted(self.channel),
            "workspace": sorted(self.workspace),
        }


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    required_capability: str
    requires_approval: bool
    description: str


TOOLS: dict[str, ToolSpec] = {
    spec.name: spec
    for spec in (
        ToolSpec("channel.read_context", "retrieval", False, "Read the channel's recent messages"),
        ToolSpec("task.create", "writing", True, "Create a task in the channel"),
        ToolSpec("artifact.write", "writing", True, "Create an artifact in the channel"),
    )
}


def allowed_tools(effective: frozenset[str]) -> list[str]:
    """The tools a run may be offered: exactly those whose capability is effective."""
    return sorted(name for name, spec in TOOLS.items() if spec.required_capability in effective)


@dataclass(frozen=True, slots=True)
class GatewayDecision:
    tool: str
    allowed: bool
    requires_approval: bool
    required_capability: str | None
    reason: str


def decide(tool: str, effective: frozenset[str]) -> GatewayDecision:
    """Permission and policy check for one tool request. Deny by default."""
    spec = TOOLS.get(tool)
    if spec is None:
        return GatewayDecision(tool, False, False, None, "unknown tool")
    if spec.required_capability not in effective:
        return GatewayDecision(
            tool,
            False,
            False,
            spec.required_capability,
            f"capability {spec.required_capability} is outside the effective set",
        )
    return GatewayDecision(
        tool,
        True,
        spec.requires_approval,
        spec.required_capability,
        "approval required" if spec.requires_approval else "allowed",
    )
