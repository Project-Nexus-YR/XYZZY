"""One real round-trip against a live model provider, exercised only when a
credential is present.

The rest of the suite verifies provider behavior against a fake HTTP
transport, which keeps CI deterministic and free. This file is the opt-in
other half: given a real ``OPENAI_API_KEY`` it drives one branch run through
the genuine provider and proves the output is model-written rather than the
SIMULATED placeholder. The ``live-provider`` workflow runs it on demand with
a repository secret; locally it runs whenever the key is exported. Without a
key it skips, loudly, rather than passing vacuously.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from multiplayer.server import create_app

pytestmark = pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY"),
    reason="live provider test needs OPENAI_API_KEY; the fake-transport suite covers the rest",
)

TOKENS = {"live-token": "user_live"}
AUTH = {"Authorization": "Bearer live-token"}


def test_a_real_provider_run_produces_non_simulated_output() -> None:
    app = create_app(":memory:", auth_tokens=TOKENS)
    with TestClient(app) as client:
        bootstrap = client.post(
            "/api/v1/me/bootstrap",
            headers=AUTH,
            json={"display_name": "Live", "room_name": "Live Check"},
        ).json()
        room_id = bootstrap["room"]["room_id"]

        template = client.get("/api/v1/agent-templates", headers=AUTH).json()[0]
        agent = client.post(
            f"/api/v1/rooms/{room_id}/agents",
            headers=AUTH,
            json={"template_id": template["template_id"]},
        ).json()

        branch = client.post(
            f"/api/v1/rooms/{room_id}/branches",
            headers=AUTH,
            json={
                "mode": "PARALLEL",
                "prompt": "In one sentence: what is a write-ahead log?",
                "agent_ids": [agent["agent_id"]],
            },
        ).json()
        run = branch["runs"][0]
        executed = client.post(
            f"/api/v1/branches/{branch['branch_id']}/runs/{run['execution_id']}/execute",
            headers=AUTH,
        )
        assert executed.status_code == 200

        outputs = client.get(f"/api/v1/rooms/{room_id}/outputs", headers=AUTH).json()
        assert outputs, "the live run produced no output at all"
        content = outputs[0]["content"]
        assert "SIMULATED WORKFLOW OUTPUT" not in content
        assert len(content.strip()) > 20
