"""What this server advertises about itself, and to whom.

A2A discovery is a JSON document at a well-known path. The specification also
describes an *extended* card that requires authentication, which is the hook this
product needs: the public document says what protocol this server speaks, and it
says nothing whatsoever about which agents exist.

That split is deliberate. A2A was written for agents reaching each other across
organisations, where advertising your skills is the entire point. Here an agent
lives inside a room whose membership is the access-control decision, so a public
list of agents and their skills would publish the shape of a private workspace to
anyone who fetched a URL. The card advertises the door; the room still decides.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .. import __version__
from ..security.capabilities import may_address
from .models import AgentAddressing, AgentInstance

# The schema's own default for a v0.3.0 server. Three components, not two:
# a client that string-compares this field would read "0.3" as a version it
# has never heard of.
PROTOCOL_VERSION = "0.3.0"

# What every agent here can take and produce unless it says otherwise. Declared
# in one place because a default that differs between the card and the
# negotiation is a promise the server does not keep.
DEFAULT_INPUT_MODES = ("text/plain",)
DEFAULT_OUTPUT_MODES = ("text/plain",)


@dataclass(frozen=True, slots=True)
class CardIdentity:
    """The deployment's own description of itself."""

    name: str = "XYZZY"
    description: str = "A multiplayer AI workspace where humans and agents decide together."
    url: str = ""
    documentation_url: str = ""


def build_public_card(identity: CardIdentity, *, sso_configured: bool) -> dict[str, Any]:
    """The unauthenticated document at the well-known path.

    Capability flags are the truth rather than an aspiration. Push notification
    is advertised as unsupported because it is unsupported: a webhook fan-out
    from a server whose whole argument is a durable ordered log would be a second
    delivery path with weaker guarantees than the one clients already have. The
    specification has a named error for asking anyway, which is what a caller
    gets.
    """
    schemes: dict[str, Any] = {
        "bearer": {
            "type": "http",
            "scheme": "bearer",
            "description": "A credential minted by this deployment, revocable as a row.",
        }
    }
    if sso_configured:
        schemes["openIdConnect"] = {
            "type": "openIdConnect",
            "description": "Sign in through this deployment's identity provider.",
        }
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "name": identity.name,
        "description": identity.description,
        "url": identity.url,
        "documentationUrl": identity.documentation_url,
        "version": __version__,
        "preferredTransport": "JSONRPC",
        # AgentCapabilities has exactly four properties in the specification, and
        # whether an extended card exists is not one of them — that lives at the
        # top level, below. Advertising it in here would be a key no conformant
        # client reads.
        "capabilities": {
            "streaming": True,
            "pushNotifications": False,
            "stateTransitionHistory": True,
            "extensions": [],
        },
        "supportsAuthenticatedExtendedCard": True,
        "defaultInputModes": list(DEFAULT_INPUT_MODES),
        "defaultOutputModes": list(DEFAULT_OUTPUT_MODES),
        # Empty on purpose, and said out loud rather than left to be inferred
        # from an absent key.
        "skills": [],
        "skillsAreListedInTheExtendedCard": True,
        "securitySchemes": schemes,
        "security": [{"bearer": []}],
    }


def _addressable_by(addressing: AgentAddressing | None, user_id: str) -> bool:
    """The same rule the invocation path enforces, asked before work starts.

    It calls `may_address` rather than restating it. An authorization rule
    written out twice is a rule that will be corrected once, and the copy that
    was not corrected is the one deciding what a stranger gets to see.

    A card listing an agent the caller cannot address would be a catalogue of
    refusals: it discloses the agent and wastes the call.
    """
    if addressing is None:
        return False
    return may_address(
        addressing.mode.value, addressing.owner_user_id, addressing.allowlist, user_id
    )


def build_extended_card(
    identity: CardIdentity,
    *,
    sso_configured: bool,
    viewer_id: str,
    agents: list[tuple[AgentInstance, AgentAddressing | None]],
) -> dict[str, Any]:
    """The authenticated document: the agents this caller may actually address.

    One caller, one card. Two people fetching this endpoint see different
    documents, because they can address different agents, and a shared cached
    copy of it would be a disclosure.
    """
    card = build_public_card(identity, sso_configured=sso_configured)
    card["skills"] = [
        {
            "id": agent.agent_id,
            "name": agent.name,
            "description": agent.role,
            "tags": sorted(agent.capabilities),
            "inputModes": list(DEFAULT_INPUT_MODES),
            "outputModes": list(DEFAULT_OUTPUT_MODES),
        }
        for agent, addressing in agents
        if _addressable_by(addressing, viewer_id)
    ]
    card.pop("skillsAreListedInTheExtendedCard", None)
    return card
