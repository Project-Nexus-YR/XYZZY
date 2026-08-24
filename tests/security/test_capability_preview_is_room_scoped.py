"""A capability preview may not read across a tenant boundary.

``GET /rooms/{room_id}/agents/{agent_id}/capabilities`` authorized the caller
against the room in the path and then resolved the terms from the *agent's own*
room. The two were never compared, so anyone who could read any room could read
any agent's channel and workspace policy anywhere: pass a room you own, name an
agent belonging to somebody else's workspace, and their policy comes back.

Nothing was granted - the effective set and the tool list were empty, because the
caller lends nothing to an agent they have no membership over. It was disclosure,
which is why every check that guards a spend still passed. The sibling addressing
route compares the two rooms; this one did not, and no test passed a mismatched
pair.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

import multiplayer.nexus_bridge.agent_bridge as bridge_module
from multiplayer.server import create_app

ALICE = {"Authorization": "Bearer alice-token"}
BOB = {"Authorization": "Bearer bob-token"}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(bridge_module, "_HAS_NEXUS", False)
    app = create_app(":memory:", auth_tokens={"alice-token": "alice", "bob-token": "bob"})
    with TestClient(app) as test_client:
        yield test_client


def _tenant(client: TestClient, who: dict[str, str], slug: str) -> tuple[str, str]:
    """A room in its own organization and workspace, with one agent in it."""
    org = client.post(
        "/api/v1/organizations", headers=who, json={"name": slug, "slug": slug}
    ).json()["org_id"]
    workspace = client.post(
        f"/api/v1/organizations/{org}/workspaces",
        headers=who,
        json={"name": slug, "slug": slug},
    ).json()["workspace_id"]
    room = client.post(
        f"/api/v1/workspaces/{workspace}/rooms", headers=who, json={"name": slug}
    ).json()["room_id"]
    template = client.get("/api/v1/agent-templates", headers=who).json()[0]["template_id"]
    agent = client.post(
        f"/api/v1/rooms/{room}/agents", headers=who, json={"template_id": template}
    ).json()["agent_id"]
    return str(room), str(agent)


def test_a_preview_cannot_name_an_agent_from_another_tenant(client: TestClient) -> None:
    alice_room, alice_agent = _tenant(client, ALICE, "alice-co")
    bob_room, _bob_agent = _tenant(client, BOB, "bob-co")

    # Bob shares no organization, workspace or room with Alice.
    assert client.get(f"/api/v1/rooms/{alice_room}/members", headers=BOB).status_code == 403

    # Alice narrows her channel policy. This is the value that leaked.
    narrowed = client.patch(
        f"/api/v1/rooms/{alice_room}/policy",
        headers=ALICE,
        json={"allowed_capabilities": ["retrieval"]},
    )
    assert narrowed.status_code == 200, narrowed.text

    # Bob authorizes against his own room and names Alice's agent.
    leaked = client.get(f"/api/v1/rooms/{bob_room}/agents/{alice_agent}/capabilities", headers=BOB)
    assert leaked.status_code == 404, leaked.text
    assert "retrieval" not in leaked.text


def test_a_preview_still_works_for_a_matching_pair(client: TestClient) -> None:
    """The gate must refuse the mismatch without refusing the ordinary request."""
    alice_room, alice_agent = _tenant(client, ALICE, "alice-two")

    allowed = client.get(
        f"/api/v1/rooms/{alice_room}/agents/{alice_agent}/capabilities", headers=ALICE
    )
    assert allowed.status_code == 200, allowed.text
    assert "terms" in allowed.json()
