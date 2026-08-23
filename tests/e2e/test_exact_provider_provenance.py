"""Acceptance proof for exact, immutable provider-request provenance."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

import multiplayer.api.routes as routes_module
import multiplayer.nexus_bridge.agent_bridge as bridge_module
from multiplayer.domain.provenance import calculate_artifact_provenance_hash
from multiplayer.model_providers import OpenAIResponsesProvider
from multiplayer.nexus_bridge.agent_bridge import NexusAgentBridge
from multiplayer.server import create_app

OWNER = {"Authorization": "Bearer owner-token"}
OUTSIDER = {"Authorization": "Bearer outsider-token"}
FAKE_CREDENTIAL = "sk-test-provenance-must-never-be-persisted"


class _ProvenanceTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        self.requests.append(payload)
        index = len(self.requests)
        text = f"Evidence-backed specialist recommendation {index}"
        # A specialist step is asked for one of the actions the run offered, so it
        # answers in that shape; synthesis is asked for prose and answers with prose.
        if payload.get("text", {}).get("format", {}).get("name") == "multiai_step":
            text = json.dumps({"action": "finish", "output": {"content": text}})
        return httpx.Response(
            200,
            request=request,
            json={
                "id": f"resp_exact_{index}",
                "output_text": text,
                "usage": {"total_tokens": 25 + index},
            },
        )


def _run_specialist(
    client: TestClient,
    room_id: str,
    template_id: str,
    instructions: str,
    prompt: str,
    intervention: str | None = None,
) -> str:
    agent = client.post(
        f"/api/v1/rooms/{room_id}/agents",
        headers=OWNER,
        json={
            "template_id": template_id,
            "system_prompt": instructions,
            "model_provider": "openai",
            "model_name": "gpt-exact-test",
        },
    ).json()
    session = client.post(
        f"/api/v1/rooms/{room_id}/agents/{agent['agent_id']}/sessions",
        headers=OWNER,
    ).json()
    execution = client.post(
        f"/api/v1/sessions/{session['session_id']}/execute", headers=OWNER
    ).json()
    if intervention is not None:
        response = client.post(
            f"/api/v1/executions/{execution['execution_id']}/intervene",
            headers=OWNER,
            json={"instruction": intervention},
        )
        assert response.status_code == 200
    result = client.post(
        f"/api/v1/executions/{execution['execution_id']}/step",
        headers=OWNER,
        json={"prompt": prompt},
    )
    assert result.status_code == 200
    return str(result.json()["output_id"])


def test_exact_provider_request_survives_output_state_and_claim_drill_down(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(bridge_module, "_HAS_NEXUS", False)
    transport = _ProvenanceTransport()
    db_path = tmp_path / "frozen-provenance.db"
    app = create_app(
        str(db_path),
        auth_tokens={"owner-token": "owner", "outsider-token": "outsider"},
    )

    with TestClient(app) as client:
        routes_module._svc_or_404().nexus = NexusAgentBridge(
            model_provider=OpenAIResponsesProvider(
                api_key=FAKE_CREDENTIAL,
                model="gpt-exact-test",
                async_transport=transport,
            )
        )
        org = client.post(
            "/api/v1/organizations",
            headers=OWNER,
            json={"name": "Acme", "slug": "exact-provider-provenance"},
        ).json()
        workspace = client.post(
            f"/api/v1/organizations/{org['org_id']}/workspaces",
            headers=OWNER,
            json={"name": "Main", "slug": "main"},
        ).json()
        room = client.post(
            f"/api/v1/workspaces/{workspace['workspace_id']}/rooms",
            headers=OWNER,
            json={"name": "Identity decision"},
        ).json()
        room_id = str(room["room_id"])
        templates = client.get("/api/v1/agent-templates", headers=OWNER).json()
        zero_claim_artifact = client.post(
            f"/api/v1/rooms/{room_id}/artifacts",
            headers=OWNER,
            json={
                "name": "Runbook",
                "artifact_type": "DOCUMENT",
                "content": "Original zero-claim runbook",
            },
        ).json()
        zero_claim_version = client.get(
            f"/api/v1/artifacts/{zero_claim_artifact['artifact_id']}/versions",
            headers=OWNER,
        ).json()[0]

        raw_prompt = "Choose the safest migration sequence.\nPreserve rollback."
        instructions = "Apply the bespoke STRIDE control checklist exactly."
        intervention = "Treat regional failover as a release blocker."
        first_id = _run_specialist(
            client,
            room_id,
            templates[0]["template_id"],
            instructions,
            raw_prompt,
            intervention,
        )
        second_id = _run_specialist(
            client,
            room_id,
            templates[1]["template_id"],
            "Challenge the migration assumptions independently.",
            "Identify the strongest counterargument.",
        )

        outputs_response = client.get(f"/api/v1/rooms/{room_id}/outputs", headers=OWNER)
        assert outputs_response.status_code == 200
        outputs = {item["output_id"]: item for item in outputs_response.json()}
        first = outputs[first_id]
        assert first["source_prompt"] == raw_prompt
        assert first["provider_name"] == "openai"
        assert first["provider_model"] == "gpt-exact-test"
        assert first["provider_response_id"] == "resp_exact_1"
        assert first["provider_interventions"] == [intervention]
        assert first["provider_evidence"] == first["content"]
        assert instructions in first["provider_input"]
        assert raw_prompt in first["provider_input"]
        assert f"- HUMAN INTERVENTION: {intervention}" in first["provider_input"]
        assert transport.requests[0]["input"] == first["provider_input"]
        assert transport.requests[0]["store"] is False
        assert FAKE_CREDENTIAL not in json.dumps(transport.requests[0])

        state = client.get(f"/api/v1/rooms/{room_id}/state", headers=OWNER).json()
        state_first = next(item for item in state["outputs"] if item["output_id"] == first_id)
        assert state_first == first

        for output_id in (first_id, second_id):
            selected = client.put(
                f"/api/v1/rooms/{room_id}/output-selections/{output_id}",
                headers=OWNER,
                json={"disposition": "INCLUDED"},
            )
            assert selected.status_code == 200
        brief = client.post(
            f"/api/v1/rooms/{room_id}/syntheses/decision-brief",
            headers=OWNER,
            json={"title": "Exact provenance decision"},
        ).json()
        provenance = client.get(
            f"/api/v1/artifact-versions/{brief['version_id']}/provenance",
            headers=OWNER,
        ).json()
        claim = next(item for item in provenance["claims"] if item["output_id"] == first_id)
        assert claim["source_prompt"] == raw_prompt
        assert claim["provider_input"] == transport.requests[0]["input"]
        assert claim["provider_name"] == "openai"
        assert claim["provider_model"] == "gpt-exact-test"
        assert claim["provider_response_id"] == "resp_exact_1"
        assert claim["provider_interventions"] == [intervention]
        assert claim["provider_evidence"] == claim["evidence"] == first["content"]
        assert provenance["provenance_hash_verified"] is True
        assert provenance["provenance_hash"] == calculate_artifact_provenance_hash(
            version_id=brief["version_id"],
            artifact_id=brief["artifact_id"],
            version_number=brief["version_number"],
            content_hash=brief["content_hash"],
            created_by=brief["created_by"],
            created_at=brief["created_at"],
            claims=provenance["claims"],
        )

        frozen_bytes = json.dumps(
            provenance, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        with sqlite3.connect(db_path) as raw_db:
            with pytest.raises(
                sqlite3.IntegrityError,
                match="artifact publication identity is immutable",
            ):
                raw_db.execute(
                    "UPDATE artifact_versions SET created_by = ? WHERE version_id = ?",
                    ("forged-author", brief["version_id"]),
                )
            with pytest.raises(
                sqlite3.IntegrityError,
                match="artifact publication identity is immutable",
            ):
                raw_db.execute(
                    "UPDATE artifact_versions SET created_at = ? WHERE version_id = ?",
                    ("1999-01-01T00:00:00+00:00", brief["version_id"]),
                )
            with pytest.raises(sqlite3.IntegrityError, match="agent_outputs are immutable"):
                raw_db.execute(
                    "UPDATE agent_outputs SET provider_name = ? WHERE output_id = ?",
                    ("rewritten-provider", first_id),
                )
            for version_id in (zero_claim_version["version_id"], brief["version_id"]):
                with pytest.raises(sqlite3.IntegrityError, match="artifact versions are immutable"):
                    raw_db.execute(
                        "INSERT OR REPLACE INTO artifact_versions("
                        "version_id, artifact_id, version_number, content, content_hash, "
                        "provenance_hash, created_by, created_at) "
                        "SELECT version_id, artifact_id, version_number, ?, content_hash, "
                        "provenance_hash, created_by, created_at FROM artifact_versions "
                        "WHERE version_id = ?",
                        ("forged replacement content", version_id),
                    )
                with pytest.raises(sqlite3.IntegrityError, match="artifact versions are immutable"):
                    raw_db.execute(
                        "DELETE FROM artifact_versions WHERE version_id = ?", (version_id,)
                    )
                with pytest.raises(sqlite3.IntegrityError, match="artifact versions are immutable"):
                    raw_db.execute(
                        "INSERT INTO artifact_versions("
                        "version_id, artifact_id, version_number, content, content_hash, "
                        "provenance_hash, created_by, created_at) "
                        "SELECT version_id, artifact_id, version_number, content, content_hash, "
                        "provenance_hash, created_by, created_at FROM artifact_versions "
                        "WHERE version_id = ?",
                        (version_id,),
                    )
            with pytest.raises(sqlite3.IntegrityError, match="artifact claims are immutable"):
                raw_db.execute(
                    "DELETE FROM artifact_claims WHERE claim_id = ?", (claim["claim_id"],)
                )
            with pytest.raises(sqlite3.IntegrityError, match="artifact claims are immutable"):
                raw_db.execute(
                    "INSERT OR REPLACE INTO artifact_claims "
                    "SELECT * FROM artifact_claims WHERE claim_id = ?",
                    (claim["claim_id"],),
                )
            with pytest.raises(
                sqlite3.IntegrityError, match="artifact claim provenance is immutable"
            ):
                raw_db.execute(
                    "DELETE FROM artifact_claim_sources WHERE claim_id = ?",
                    (claim["claim_id"],),
                )
            with pytest.raises(
                sqlite3.IntegrityError, match="artifact claim provenance is immutable"
            ):
                raw_db.execute(
                    "INSERT OR REPLACE INTO artifact_claim_sources "
                    "SELECT * FROM artifact_claim_sources WHERE claim_id = ?",
                    (claim["claim_id"],),
                )
            with pytest.raises(sqlite3.IntegrityError, match="agent_outputs are immutable"):
                raw_db.execute("DELETE FROM agent_outputs WHERE output_id = ?", (first_id,))
            with pytest.raises(sqlite3.IntegrityError, match="agent_outputs are immutable"):
                raw_db.execute(
                    "INSERT OR REPLACE INTO agent_outputs "
                    "SELECT * FROM agent_outputs WHERE output_id = ?",
                    (first_id,),
                )
            # Simulate a future migration or defect bypassing the defense-in-depth
            # trigger: the published per-version snapshot must still be isolated.
            raw_db.execute("DROP TRIGGER agent_outputs_reject_update")
            raw_db.execute(
                "UPDATE agent_outputs SET provider_name = ?, provider_input = ? "
                "WHERE output_id = ?",
                ("rewritten-provider", "rewritten-input", first_id),
            )
            raw_db.commit()

        frozen_after = client.get(
            f"/api/v1/artifact-versions/{brief['version_id']}/provenance",
            headers=OWNER,
        ).json()
        assert (
            json.dumps(
                frozen_after,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            == frozen_bytes
        )
        denied = client.get(
            f"/api/v1/artifact-versions/{brief['version_id']}/provenance",
            headers=OUTSIDER,
        )
        assert denied.status_code == 403

        # Even if a privileged migration removes the content-update guard, the
        # verifier independently hashes the stored content bytes before trusting
        # the committed content_hash/provenance_hash pair.
        with sqlite3.connect(db_path) as raw_db:
            raw_db.execute("DROP TRIGGER artifact_versions_reject_content_update")
            raw_db.execute(
                "UPDATE artifact_versions SET content = ? WHERE version_id = ?",
                ("forged content with stale hashes", zero_claim_version["version_id"]),
            )
            raw_db.commit()
        tampered = client.get(
            f"/api/v1/artifact-versions/{zero_claim_version['version_id']}/provenance",
            headers=OWNER,
        ).json()
        assert tampered["claims"] == []
        assert tampered["provenance_hash_verified"] is False
        assert FAKE_CREDENTIAL not in json.dumps(outputs_response.json())
        assert FAKE_CREDENTIAL not in json.dumps(state)
        assert FAKE_CREDENTIAL not in json.dumps(provenance)
