"""The five low-severity api-track findings that are not their own file:

- finding 33: GET /rooms/{room_id}/branches re-read the whole room's outputs
  and selections once per branch; it now reads each once for the page.
- finding 35: POST /rooms/{room_id}/approvals took an empty, unbounded
  `action` off the query string; ontology's `corrected_properties` had no
  type or key-count bound.
- finding 36: POST /rooms/{room_id}/join was gated on MUTATE while the
  service itself only requires READ, so an invited viewer got 403 joining
  their own room.

Finding 13 (DELETE /workspaces/{id}/members/{user_id}) is its own file, since
it depends on a method the runtime track has not merged yet. Finding 34
(DomainError status-by-class) needed a change to domain/models.py, outside
this track's owned files; see the report's "Needs lead wiring" for it —
nothing here exercises it, since nothing here changed for it.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from multiplayer.server import create_app
from multiplayer.services.service import MultiplayerService

TOKENS = {"owner-token": "user_1", "viewer-token": "user_viewer"}
OWNER = {"Authorization": "Bearer owner-token"}
VIEWER = {"Authorization": "Bearer viewer-token"}


def _bootstrap_room(client: TestClient) -> str:
    bootstrap = client.post(
        "/api/v1/me/bootstrap",
        headers=OWNER,
        json={"display_name": "Owner", "room_name": "Room"},
    )
    assert bootstrap.status_code == 200, bootstrap.text
    return str(bootstrap.json()["room"]["room_id"])


# -- Finding 36: join needs READ, not MUTATE ---------------------------------


def test_an_invited_viewer_can_join_their_own_room() -> None:
    """Fails before the fix: routes.py gated join on MUTATE, which a viewer
    role does not carry, so this answered 403 despite the service (and
    leave_room beside it) only ever requiring READ.
    """
    app = create_app(":memory:", auth_tokens=TOKENS)
    with TestClient(app) as client:
        room_id = _bootstrap_room(client)
        invite = client.post(
            f"/api/v1/rooms/{room_id}/members/invitations",
            headers=OWNER,
            json={"user_id": "user_viewer", "role": "viewer"},
        )
        assert invite.status_code == 200, invite.text

        joined = client.post(f"/api/v1/rooms/{room_id}/join", headers=VIEWER)
        assert joined.status_code == 200, joined.text
        assert joined.json() == {"status": "joined"}


# -- Finding 35: the approvals query string and the ontology properties bound -


def _seed_approval_prereqs(client: TestClient) -> dict[str, str]:
    room_id = _bootstrap_room(client)
    templates = client.get("/api/v1/agent-templates", headers=OWNER).json()
    agent = client.post(
        f"/api/v1/rooms/{room_id}/agents",
        headers=OWNER,
        json={"template_id": templates[0]["template_id"]},
    ).json()
    session = client.post(
        f"/api/v1/rooms/{room_id}/agents/{agent['agent_id']}/sessions", headers=OWNER
    ).json()
    execution = client.post(
        f"/api/v1/sessions/{session['session_id']}/execute", headers=OWNER
    ).json()
    return {
        "room_id": room_id,
        "agent_id": agent["agent_id"],
        "execution_id": execution["execution_id"],
    }


def test_a_blank_approval_action_is_refused() -> None:
    """Fails before the fix: `action: str = Query("")` accepted and stored an
    empty action description with no validation at all.
    """
    app = create_app(":memory:", auth_tokens=TOKENS)
    with TestClient(app) as client:
        seeded = _seed_approval_prereqs(client)
        for blank in ("", "   "):
            response = client.post(
                f"/api/v1/rooms/{seeded['room_id']}/approvals",
                headers=OWNER,
                params={
                    "execution_id": seeded["execution_id"],
                    "agent_id": seeded["agent_id"],
                    "action": blank,
                },
            )
            assert response.status_code == 400, response.text


def test_an_oversized_approval_action_is_refused_by_the_query_bound() -> None:
    """Fails before the fix: `action` carried no `max_length` at all."""
    app = create_app(":memory:", auth_tokens=TOKENS)
    with TestClient(app) as client:
        seeded = _seed_approval_prereqs(client)
        response = client.post(
            f"/api/v1/rooms/{seeded['room_id']}/approvals",
            headers=OWNER,
            params={
                "execution_id": seeded["execution_id"],
                "agent_id": seeded["agent_id"],
                "action": "x" * 10_001,
            },
        )
        assert response.status_code == 422


def test_a_well_formed_approval_action_still_works() -> None:
    app = create_app(":memory:", auth_tokens=TOKENS)
    with TestClient(app) as client:
        seeded = _seed_approval_prereqs(client)
        response = client.post(
            f"/api/v1/rooms/{seeded['room_id']}/approvals",
            headers=OWNER,
            params={
                "execution_id": seeded["execution_id"],
                "agent_id": seeded["agent_id"],
                "action": "Publish the migration",
            },
        )
        assert response.status_code == 200, response.text


def test_corrected_properties_rejects_a_key_count_over_the_bound() -> None:
    """Fails before the fix: `corrected_properties: dict[str, Any] | None` had
    no bound at all, so an unbounded object landed in the ontology row.
    """
    app = create_app(":memory:", auth_tokens=TOKENS)
    with TestClient(app) as client:
        room_id = _bootstrap_room(client)
        from multiplayer.api import routes

        svc = routes._svc
        assert svc is not None

        # Ontology review needs an entity to review; seed one directly rather
        # than driving a whole extraction pipeline through the API.
        from multiplayer.domain.models import (
            OntologyDerivationKind,
            OntologyEntity,
            OntologyEntityKind,
            OntologyExtractor,
            OntologyReviewStatus,
        )

        async def _seed_entity() -> str:
            entity = OntologyEntity(
                entity_id="ont-ent-1",
                room_id=room_id,
                kind=OntologyEntityKind.CLAIM,
                source_object_id="src-1",
                label="thing",
                properties={},
                derivation_kind=OntologyDerivationKind.AI_DERIVED,
                confidence=0.9,
                evidence_ids=("src-1",),
                source_ids=("src-1",),
                review_status=OntologyReviewStatus.UNCONFIRMED,
                extractor=OntologyExtractor.ASYNC,
                asserted_at_sequence=1,
                evidence_event_sequences=(1,),
            )
            async with svc.db.transaction():
                await svc.repos.ontology.materialize_in_transaction([entity], [])
            return entity.entity_id

        entity_id = asyncio.run(_seed_entity())

        too_many_keys = {f"k{i}": i for i in range(51)}
        response = client.post(
            f"/api/v1/rooms/{room_id}/ontology/entities/{entity_id}/reviews",
            headers=OWNER,
            json={"action": "correct", "corrected_properties": too_many_keys},
        )
        assert response.status_code == 422


# -- Finding 33: the branches route reads outputs/selections once per page --


async def test_branches_route_reads_room_outputs_and_selections_once_per_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fails before the fix: `_branch_token_usage_total` called
    `svc.list_room_outputs`/`svc.list_output_selections` once per branch, so
    N branches cost N room-wide scans instead of one.

    The branches themselves are synthetic (`MultiplayerService.list_room_branches`
    monkeypatched to return them), the same way `test_fix_api_pagination.py`
    covers this route's `limit`: what is under test is `routes.py`'s own call
    count, not the real branch-creation pipeline.
    """
    from multiplayer.domain.models import Branch, BranchMode, BranchStatus

    app = create_app(":memory:", auth_tokens=TOKENS)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            bootstrap = await client.post(
                "/api/v1/me/bootstrap",
                headers=OWNER,
                json={"display_name": "Owner", "room_name": "Room"},
            )
            room_id = bootstrap.json()["room"]["room_id"]

            branches = [
                Branch(
                    branch_id=f"branch-{i}",
                    room_id=room_id,
                    mode=BranchMode.TURN_LOCKED_SINGLE,
                    status=BranchStatus.COMPLETED,
                    initiated_by="user_1",
                    initiating_prompt="p",
                    context_event_sequence=0,
                    context_message_ids=(),
                    context_snapshot={},
                    context_hash="h",
                )
                for i in range(3)
            ]

            calls = {"outputs": 0, "selections": 0}

            async def fake_list_room_branches(
                self: MultiplayerService, rid: str, *, limit: int | None = None
            ) -> list[Branch]:
                return branches[:limit]

            async def counted_outputs(self: MultiplayerService, rid: str) -> list[Any]:
                calls["outputs"] += 1
                return []

            async def counted_selections(self: MultiplayerService, rid: str) -> list[Any]:
                calls["selections"] += 1
                return []

            async def empty_runs(self: MultiplayerService, branch_id: str) -> list[Any]:
                return []

            monkeypatch.setattr(MultiplayerService, "list_room_branches", fake_list_room_branches)
            monkeypatch.setattr(MultiplayerService, "list_room_outputs", counted_outputs)
            monkeypatch.setattr(MultiplayerService, "list_output_selections", counted_selections)
            monkeypatch.setattr(MultiplayerService, "list_branch_runs", empty_runs)

            from multiplayer.api import routes

            svc = routes._svc
            assert svc is not None

            async def fake_list_by_branch(branch_id: str) -> list[Any]:
                return []

            monkeypatch.setattr(svc.repos.branch_syntheses, "list_by_branch", fake_list_by_branch)

            listing = await client.get(f"/api/v1/rooms/{room_id}/branches", headers=OWNER)
            assert listing.status_code == 200
            assert len(listing.json()) == 3
            assert calls["outputs"] == 1, calls
            assert calls["selections"] == 1, calls
