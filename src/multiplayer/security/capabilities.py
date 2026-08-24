"""Deterministic five-way capability enforcement and the tool gateway registry.

PRD §13: effective capabilities = user ∩ agent ∩ skill ∩ channel ∩ workspace.
PRD §14: every tool request passes permission check, policy check, approval if
required, execution, and an audit event; unauthorized tools are unavailable or
rejected. Everything here is a pure function of durable records.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace

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

    def bounded_by(self, authority: frozenset[str]) -> CapabilityTerms:
        """These terms narrowed by a second principal's authority over the same run.

        The user term is the principal-side ceiling, so a delegate or an intervener
        lands there: whoever steers a run can only ever lower it.
        """
        return replace(self, user=self.user & authority)

    def as_dict(self) -> dict[str, list[str]]:
        return {
            "user": sorted(self.user),
            "agent": sorted(self.agent),
            "skill": sorted(self.skill),
            "channel": sorted(self.channel),
            "workspace": sorted(self.workspace),
        }


@dataclass(frozen=True, slots=True)
class UnboundedTerms:
    """One principal's five terms, before the steerers of a run have narrowed them.

    It deliberately has no ``effective``. A set that a tool decision may spend comes
    out of :meth:`bounded_by` and nowhere else, so a spend-point that derives terms
    and forgets the steerers has nothing to hand the gateway: the omission is a type
    error rather than a quietly wider set. That is the whole reason this wrapper
    exists — the bound had been applied by remembering to apply it, and remembering
    failed at a new place every round.

    :meth:`lendable` is the other exit, and it is not a spend: it is what one
    principal may lend an agent, which is what a launch gate asks and what each
    steerer contributes as one of the bounds above.
    """

    terms: CapabilityTerms

    def bounded_by(self, authorities: Iterable[frozenset[str]]) -> CapabilityTerms:
        """These terms narrowed by every authority that steers the same run."""
        bounded = self.terms
        for authority in authorities:
            bounded = bounded.bounded_by(authority)
        return bounded

    def lendable(self) -> frozenset[str]:
        """What this principal may lend an agent here. A gate, never a tool decision."""
        return self.terms.effective

    def as_dict(self) -> dict[str, list[str]]:
        return self.terms.as_dict()


@dataclass(frozen=True, slots=True)
class RunAuthorization:
    """What a tool writer re-derives its terms from, inside its own transaction.

    Identity and addressing are gates that already refused earlier; this carries only
    the authority a write still has to be checked against. A human caller passes
    ``None`` instead and is guarded by the room membership check beside it.

    ``steerers`` names everyone who has steered this run. It is on the authorization
    rather than threaded to each spend-point because the twelve relocations of one
    defect all had the same shape: a spend-point re-derived the five terms correctly
    and did not know it also owed the steerer bound. A spend-point cannot know that,
    and should not have to — it consumes this object, the object names the steerers,
    and the single derivation that reads it applies them. The names are records, not
    authority: what each of them may lend is read from durable rows at the moment it
    is spent.
    """

    run_id: str
    agent_id: str
    room_id: str
    authorized_by: str
    acting_user_id: str
    required_capability: str
    steerers: frozenset[str]


def may_address(
    mode: str,
    owner_user_id: str,
    allowlist: frozenset[str],
    user_id: str,
) -> bool:
    """Whether this human may point this agent. Deny by default.

    Addressing gates who may point an agent, not what it does: a wider mode never
    adds a capability, and NOBODY parks the agent with its history still readable.
    """
    if not user_id:
        return False
    if mode == "ANYONE":
        return True
    if mode == "OWNER_ONLY":
        return user_id == owner_user_id
    if mode == "ALLOWLIST":
        return user_id == owner_user_id or user_id in allowlist
    return False


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
        # A reaction is a durable, agent-attributed write into the channel, so it is
        # gated on writing — the one capability a viewer may never lend — but it
        # needs no approval, because unlike a task or an artifact it is one glyph
        # the agent can take back.
        ToolSpec("message.react", "writing", False, "React to a message as this agent"),
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
