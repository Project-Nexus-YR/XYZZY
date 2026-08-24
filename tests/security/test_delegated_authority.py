"""One agent asking another can never obtain what the asker was refused.

This is the property the whole delegation design exists for, and it is asserted
against the *existing* derivation rather than a parallel one: a delegating agent
is written into the bounding set like any other principal, so every spend-point
in the codebase ceilings the delegate without having learned a new name.

That shape was chosen because this codebase relocated one defect thirteen times,
every time by enumerating participants one at a time and coming up one short. If
delegation had added an argument at the call sites, it would have been the
fourteenth.
"""

from __future__ import annotations

import pytest

from multiplayer.db.connection import Database
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.security.capabilities import BoundingPrincipals, agent_principal
from multiplayer.services.service import MultiplayerService

OWNER = "owner"


@pytest.fixture
async def service():
    db = Database(":memory:")
    await db.connect()
    svc = MultiplayerService(db, RealtimeHub(), known_users=frozenset({OWNER}))
    await svc.initialize()
    yield svc
    await db.close()


async def _room(svc: MultiplayerService) -> str:
    org = await svc.create_organization("Delegation org", "deleg-org", OWNER)
    workspace = await svc.create_workspace(org.org_id, "Main", "main", OWNER)
    room = await svc.create_room(workspace.workspace_id, "Decision", OWNER)
    return room.room_id


async def _agent(svc: MultiplayerService, room_id: str, template_name: str) -> str:
    templates = await svc.list_agent_templates()
    agent = await svc.spawn_agent(
        room_id,
        next(t.template_id for t in templates if t.name == template_name),
        name=template_name,
        requested_by=OWNER,
    )
    return agent.agent_id


@pytest.mark.asyncio
async def test_a_delegate_is_ceilinged_by_the_agent_that_asked(service):
    room_id = await _room(service)
    # The Coder holds coding, review and testing. The Researcher holds analysis,
    # research and retrieval. Neither is a subset of the other, so a delegation
    # between them has something real to take away.
    asker = await _agent(service, room_id, "Researcher")
    delegate = await _agent(service, room_id, "Coder")

    delegate_agent = await service.get_agent(delegate)
    alone = await service._lendable_terms(
        delegate_agent, room_id, BoundingPrincipals(frozenset({OWNER}))
    )
    delegated = await service._lendable_terms(
        delegate_agent,
        room_id,
        BoundingPrincipals(frozenset({OWNER})).also_bounded_by({agent_principal(asker)}),
    )

    asker_agent = await service.get_agent(asker)
    # Whatever the delegate can be lent under the asker, the asker holds itself.
    assert delegated.lendable() <= frozenset(asker_agent.capabilities)
    # And it is strictly less than what a human alone could lend it, because the
    # asker's own set does not cover the delegate's.
    assert delegated.lendable() < alone.lendable()


@pytest.mark.asyncio
async def test_naming_the_asker_can_only_ever_narrow(service):
    room_id = await _room(service)
    asker = await _agent(service, room_id, "Architect")
    delegate = await _agent(service, room_id, "Architect")
    delegate_agent = await service.get_agent(delegate)

    alone = await service._lendable_terms(
        delegate_agent, room_id, BoundingPrincipals(frozenset({OWNER}))
    )
    delegated = await service._lendable_terms(
        delegate_agent,
        room_id,
        BoundingPrincipals(frozenset({OWNER})).also_bounded_by({agent_principal(asker)}),
    )
    # Two agents of the same template: nothing is taken away, and nothing is
    # added. A bounding set that could add would be an escalation dressed as a
    # delegation.
    assert delegated.lendable() == alone.lendable()


@pytest.mark.asyncio
async def test_an_asker_that_left_the_room_lends_nothing(service):
    room_id = await _room(service)
    asker = await _agent(service, room_id, "Researcher")
    delegate = await _agent(service, room_id, "Researcher")
    delegate_agent = await service.get_agent(delegate)

    bounding = BoundingPrincipals(frozenset({OWNER})).also_bounded_by({agent_principal(asker)})
    assert (await service._lendable_terms(delegate_agent, room_id, bounding)).lendable()

    await service.remove_agent_from_room(asker, room_id, OWNER)
    # Re-derived at the moment of spending, from durable rows. Removing the asker
    # stops the delegate too, rather than leaving it running on authority its
    # asker no longer has.
    assert not (await service._lendable_terms(delegate_agent, room_id, bounding)).lendable()


@pytest.mark.asyncio
async def test_a_chain_of_askers_intersects_all_of_them(service):
    room_id = await _room(service)
    first = await _agent(service, room_id, "Researcher")
    second = await _agent(service, room_id, "Synthesizer")
    delegate = await _agent(service, room_id, "Architect")
    delegate_agent = await service.get_agent(delegate)

    chain = (
        BoundingPrincipals(frozenset({OWNER}))
        .also_bounded_by({agent_principal(first)})
        .also_bounded_by({agent_principal(second)})
    )
    lendable = (await service._lendable_terms(delegate_agent, room_id, chain)).lendable()

    for asker_id in (first, second):
        asker = await service.get_agent(asker_id)
        assert lendable <= frozenset(asker.capabilities)
    # Researcher ∩ Synthesizer ∩ Architect is analysis alone, and the delegate
    # cannot recover anything by being asked through a longer chain.
    assert lendable <= {"analysis"}


@pytest.mark.asyncio
async def test_a_human_whose_id_resembles_an_agent_is_still_read_as_a_human(service):
    room_id = await _room(service)
    delegate = await _agent(service, room_id, "Coder")
    delegate_agent = await service.get_agent(delegate)

    # An agent principal is marked by its prefix, not guessed from its shape. A
    # user id that merely looks like an agent id must not be resolved against the
    # agents table — that would be an identity confusion, and the prefix is what
    # makes it impossible.
    lookalike = f"agent_{delegate.split('_', 1)[1]}"
    bounding = BoundingPrincipals(frozenset({OWNER, lookalike}))
    lendable = (await service._lendable_terms(delegate_agent, room_id, bounding)).lendable()

    # Read as a human with no membership, so it lends nothing — deny by default,
    # rather than quietly resolving to the agent's own capabilities.
    assert not lendable
