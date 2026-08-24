"""What the discovery document tells a stranger, and what it does not.

The specification's own example is a public card advertising an agent's skills,
because it was written for agents finding each other across organisations. Here
the same document would publish the shape of a private workspace, so the public
card carries no agents at all and the authenticated one carries only the agents
that particular caller could actually address.
"""

from __future__ import annotations

from multiplayer.domain.agent_card import (
    CardIdentity,
    build_extended_card,
    build_public_card,
)
from multiplayer.domain.models import AddressingMode, AgentAddressing, AgentInstance

IDENTITY = CardIdentity(url="https://xyzzy.example")


def _agent(agent_id: str, name: str) -> AgentInstance:
    return AgentInstance(
        agent_id=agent_id,
        template_id="tpl",
        room_id="room-1",
        name=name,
        role="analyst",
        capabilities=frozenset({"retrieval"}),
    )


def _addressing(agent_id: str, mode: AddressingMode, **kwargs: object) -> AgentAddressing:
    return AgentAddressing(
        agent_id=agent_id,
        room_id="room-1",
        mode=mode,
        owner_user_id=str(kwargs.get("owner", "alice")),
        allowlist=frozenset(kwargs.get("allowlist", frozenset())),  # type: ignore[arg-type]
    )


def test_the_public_card_names_the_protocol_and_no_agents():
    card = build_public_card(IDENTITY, sso_configured=False)
    assert card["skills"] == []
    assert card["supportsAuthenticatedExtendedCard"] is True
    assert card["preferredTransport"] == "JSONRPC"


def test_the_card_carries_every_field_the_schema_requires():
    # A card missing a required field is a card a conformant client rejects
    # before it reads a word of it, and the failure arrives as a validation
    # error on the far side rather than as anything this deployment logs.
    card = build_public_card(IDENTITY, sso_configured=False)
    required = {
        "capabilities",
        "defaultInputModes",
        "defaultOutputModes",
        "description",
        "name",
        "protocolVersion",
        "skills",
        "url",
        "version",
    }
    assert required <= card.keys()
    assert card["protocolVersion"] == "0.3.0"


def test_capabilities_carries_the_four_properties_the_schema_defines_and_no_others():
    # AgentCapabilities is a closed vocabulary in the specification. An extra key
    # in here is not additive: it reads as a capability to a human and as nothing
    # at all to a client, which is the worst of both.
    card = build_public_card(IDENTITY, sso_configured=False)
    assert set(card["capabilities"]) == {
        "streaming",
        "pushNotifications",
        "stateTransitionHistory",
        "extensions",
    }


def test_push_notification_is_advertised_as_unsupported_rather_than_stubbed():
    # Claiming a capability and then failing on it is worse than declining it:
    # the specification has a named error precisely so a caller can plan around
    # the absence instead of discovering it mid-task.
    card = build_public_card(IDENTITY, sso_configured=False)
    assert card["capabilities"]["pushNotifications"] is False
    assert card["capabilities"]["streaming"] is True


def test_the_card_offers_sign_in_only_where_sign_in_exists():
    assert (
        "openIdConnect" not in build_public_card(IDENTITY, sso_configured=False)["securitySchemes"]
    )
    assert "openIdConnect" in build_public_card(IDENTITY, sso_configured=True)["securitySchemes"]


def test_the_extended_card_shows_each_caller_only_what_they_could_address():
    agents = [
        (_agent("a-anyone", "Open"), _addressing("a-anyone", AddressingMode.ANYONE)),
        (_agent("a-owner", "Alice's"), _addressing("a-owner", AddressingMode.OWNER_ONLY)),
        (
            _agent("a-list", "Listed"),
            _addressing("a-list", AddressingMode.ALLOWLIST, allowlist={"bob"}),
        ),
        (_agent("a-parked", "Parked"), _addressing("a-parked", AddressingMode.NOBODY)),
        (_agent("a-unset", "Unset"), None),
    ]

    def skills_for(viewer: str) -> set[str]:
        card = build_extended_card(IDENTITY, sso_configured=False, viewer_id=viewer, agents=agents)
        return {skill["id"] for skill in card["skills"]}

    assert skills_for("alice") == {"a-anyone", "a-owner", "a-list"}
    # Bob is on one allowlist and owns nothing.
    assert skills_for("bob") == {"a-anyone", "a-list"}
    assert skills_for("carol") == {"a-anyone"}

    # A parked agent and one with no addressing row are invisible to everyone,
    # rather than listed and then refused on use.
    for viewer in ("alice", "bob", "carol"):
        assert "a-parked" not in skills_for(viewer)
        assert "a-unset" not in skills_for(viewer)


def test_two_callers_do_not_share_one_card():
    agents = [(_agent("a-owner", "Alice's"), _addressing("a-owner", AddressingMode.OWNER_ONLY))]
    mine = build_extended_card(IDENTITY, sso_configured=False, viewer_id="alice", agents=agents)
    theirs = build_extended_card(IDENTITY, sso_configured=False, viewer_id="mallory", agents=agents)
    assert mine["skills"] and not theirs["skills"]
