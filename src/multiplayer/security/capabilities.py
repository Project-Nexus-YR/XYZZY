"""Deterministic five-way capability enforcement and the tool gateway registry.

PRD §13: effective capabilities = user ∩ agent ∩ skill ∩ channel ∩ workspace.
PRD §14: every tool request passes permission check, policy check, approval if
required, execution, and an audit event; unauthorized tools are unavailable or
rejected. Everything here is a pure function of durable records.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, replace
from enum import StrEnum

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


# A run whose durable rows name nobody is authorized by nobody. The empty name
# matches no room membership, so it lends nothing, which is what an unknown
# principal must lend.
UNKNOWN_PRINCIPAL = ""

# A delegating agent is a principal like any other, and is written into a
# bounding set under this prefix so it can never be mistaken for a human id.
# The union that fills a bounding set said a fourth kind of participant would be
# a fourth arm of it and nothing else; this is that fourth kind.
AGENT_PRINCIPAL_PREFIX = "agent:"


def agent_principal(agent_id: str) -> str:
    """How a delegating agent appears in a bounding set."""
    return f"{AGENT_PRINCIPAL_PREFIX}{agent_id}"


def delegating_agent_id(principal: str) -> str | None:
    """The agent behind a principal, or None when it names a human."""
    if principal.startswith(AGENT_PRINCIPAL_PREFIX):
        return principal[len(AGENT_PRINCIPAL_PREFIX) :] or None
    return None


@dataclass(frozen=True, slots=True)
class BoundingPrincipals:
    """Everyone whose grant bounds one run: one set, never a field per kind.

    Thirteen rounds relocated one defect, and the last two were the same mistake in
    two costumes. Round eight put the steerers on the authorization and left the
    acting caller off it, so a caller narrowed to nothing while an approval waited
    still spent the grant they had when they stepped. Enumerating identities one at a
    time is what failed: the fix was always one short of the participants a run has.

    So there is one field, and it holds all of them. A spend-point cannot pick a
    principal out of this and bound by that alone, because there is no field to pick;
    a new kind of participant is a new durable row in the one union that fills this,
    not a new argument at every door that already forgot the last one.

    Empty is refused. The intersection over no principals is the whole vocabulary,
    which is the one value this must never quietly mean.
    """

    principals: frozenset[str]

    def __post_init__(self) -> None:
        if not self.principals:
            raise ValueError("a run bounded by no principal at all is bounded by nothing")

    @classmethod
    def read_from(cls, principals: Iterable[str]) -> BoundingPrincipals:
        """Whatever the durable rows named. Rows that named nobody are an unknown."""
        return cls(frozenset(principals) or frozenset({UNKNOWN_PRINCIPAL}))

    def also_bounded_by(self, principals: Iterable[str]) -> BoundingPrincipals:
        """The same decision with further principals' grants over it. A union, only.

        Some principals bound one call rather than the run it belongs to: a reviewer
        releasing a parked tool call is answering for that call, and binding her into
        every later call of the run is a reach nobody asked her for. She still has to
        be read where that call is decided and again inside the transaction that
        writes it, so she needs to reach the bound — just not the run's own set.

        The one operation offered, and it adds. There is no expression here that drops
        a principal, so a set built through this is never narrower than the durable
        rows named, and since the terms are an intersection over the set, a wider set
        is always a narrower grant. That is what keeps this incapable of being the
        escalation it sits next to.
        """
        return BoundingPrincipals(self.principals | frozenset(principals))

    def __iter__(self) -> Iterator[str]:
        return iter(sorted(self.principals))


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
class RunAuthorization:
    """What a tool writer re-derives its terms from, inside its own transaction.

    Identity and addressing are gates that already refused earlier; this carries only
    the authority a write still has to be checked against. A human caller passes
    ``None`` instead and is guarded by the room membership check beside it.

    It names its principals once, as a whole set, because the thirteen relocations of
    one defect were all an enumeration that came up short: a spend-point re-derived
    the five terms correctly and did not know which further identity it also owed. A
    spend-point cannot know that, and should not have to — it consumes this object,
    the object carries every principal, and the single derivation that reads it
    intersects all of them. The names are records, not authority: what each may lend
    is read from durable rows at the moment it is spent.
    """

    run_id: str
    agent_id: str
    room_id: str
    bounding: BoundingPrincipals
    required_capability: str


@dataclass(frozen=True, slots=True)
class UnboundedTerms:
    """The five terms read for one set of principals, before anything may spend them.

    It deliberately has no ``effective``. A set that a tool decision may spend comes
    out of :meth:`spend_under` and nowhere else, and that method demands the
    authorization whose principals these terms were read for. So terms derived for a
    narrower set than the run actually has cannot be handed to a gateway at all: the
    omission is a refusal rather than a quietly wider set. That is the whole reason
    this wrapper exists — the bound had been applied by remembering to apply it, and
    remembering failed at a new place every round.

    :meth:`lendable` is the other exit, and it is not a spend: it is what these
    principals may lend an agent here, which is what a launch or steer gate asks.
    """

    bounding: BoundingPrincipals
    terms: CapabilityTerms

    def spend_under(self, authorization: RunAuthorization) -> CapabilityTerms:
        """The one exit to a spendable set, and only for the run these terms are of."""
        if authorization.bounding != self.bounding:
            raise ValueError(
                f"terms bounded by {sorted(self.bounding)} may not be spent under run "
                f"{authorization.run_id}, which is bounded by {sorted(authorization.bounding)}"
            )
        return self.terms

    def lendable(self) -> frozenset[str]:
        """What these principals may lend an agent here. A gate, never a tool decision."""
        return self.terms.effective

    def as_dict(self) -> dict[str, list[str]]:
        return self.terms.as_dict()


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


class Posture(StrEnum):
    """How much of what a channel already permits stops at a human.

    Two, because two is what the records here can tell apart. ``GUARDED`` is what
    every channel has always been: the five-way intersection decides what an agent
    may do, and the per-tool floor decides what pauses. ``STRICT`` adds one rule —
    every call pauses — and takes nothing away, because there is no tier below
    ``GUARDED`` and the floor holds under both.
    """

    GUARDED = "GUARDED"
    STRICT = "STRICT"


STRICT_PAUSE_REASON = "strict posture: every call in this channel pauses for a human"


def under_posture(decision: GatewayDecision, posture: Posture) -> GatewayDecision:
    """Raise whether a permitted call pauses. It cannot reach ``allowed`` at all.

    Not a branch that happens not to write ``allowed``: the only value returned other
    than the decision itself is a copy naming ``requires_approval`` and ``reason``, so
    what a posture may do to a decision is the whole of what this can express. A
    posture is therefore structurally incapable of permitting anything the
    intersection refused, which is the one property it must have.

    A refused decision is returned untouched rather than offered to a reviewer.
    "Ask a human" applied to a denial would be a widening through the back door: the
    call would be permitted by whoever answered instead of by the records.
    """
    if posture is not Posture.STRICT or not decision.allowed or decision.requires_approval:
        return decision
    return replace(decision, requires_approval=True, reason=STRICT_PAUSE_REASON)
