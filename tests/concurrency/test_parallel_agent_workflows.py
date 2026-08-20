"""Regression coverage for concurrent browser-style specialist launches."""

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from multiplayer.server import create_app


@pytest.mark.asyncio
async def test_three_parallel_agent_workflows_persist_outputs_events_and_reconnect_state():
    """Promise.all-style spawn -> session -> execution -> step cannot share a transaction."""
    app = create_app(":memory:", auth_tokens={"owner-token": "user_1"})
    transport = ASGITransport(app=app)
    headers = {"Authorization": "Bearer owner-token"}

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            headers=headers,
        ) as client:
            org_response = await client.post(
                "/api/v1/organizations", json={"name": "Acme", "slug": "acme"}
            )
            assert org_response.status_code == 200
            org = org_response.json()
            workspace_response = await client.post(
                f"/api/v1/organizations/{org['org_id']}/workspaces",
                json={"name": "Main", "slug": "main"},
            )
            assert workspace_response.status_code == 200
            workspace = workspace_response.json()
            room_response = await client.post(
                f"/api/v1/workspaces/{workspace['workspace_id']}/rooms",
                json={"name": "Architecture decision"},
            )
            assert room_response.status_code == 200
            room_id = room_response.json()["room_id"]
            templates_response = await client.get("/api/v1/agent-templates")
            assert templates_response.status_code == 200
            templates = templates_response.json()[:3]

            async def run_specialist(index: int, template_id: str) -> dict[str, str]:
                spawn = await client.post(
                    f"/api/v1/rooms/{room_id}/agents",
                    json={"template_id": template_id, "name": f"Specialist {index}"},
                )
                assert spawn.status_code == 200, spawn.text
                agent_id = spawn.json()["agent_id"]

                session = await client.post(f"/api/v1/rooms/{room_id}/agents/{agent_id}/sessions")
                assert session.status_code == 200, session.text
                session_id = session.json()["session_id"]

                execution = await client.post(f"/api/v1/sessions/{session_id}/execute")
                assert execution.status_code == 200, execution.text
                execution_id = execution.json()["execution_id"]

                step = await client.post(
                    f"/api/v1/executions/{execution_id}/step",
                    json={"prompt": f"Analyze option {index}"},
                )
                assert step.status_code == 200, step.text
                return {
                    "agent_id": agent_id,
                    "session_id": session_id,
                    "execution_id": execution_id,
                    "output_id": step.json()["output_id"],
                }

            workflows = await asyncio.gather(
                *(
                    run_specialist(index, template["template_id"])
                    for index, template in enumerate(templates, start=1)
                )
            )

            state_response = await client.get(f"/api/v1/rooms/{room_id}/state")
            assert state_response.status_code == 200
            state = state_response.json()
            assert {item["output_id"] for item in workflows} == {
                output["output_id"] for output in state["outputs"]
            }
            assert len(state["outputs"]) == 3
            assert len(state["runs"]) == 3
            assert {run["status"] for run in state["runs"]} == {"COMPLETED"}

            events = state["events_since"]
            sequences = [event["sequence"] for event in events]
            assert sequences == list(range(1, len(events) + 1))
            assert len({event["event_id"] for event in events}) == len(events)
            assert (
                len([event for event in events if event["event_type"] == "agent.output.created"])
                == 3
            )

            reconnect = await client.get(
                f"/api/v1/rooms/{room_id}/state",
                params={"last_sequence": sequences[-1]},
            )
            assert reconnect.status_code == 200
            reconnect_state = reconnect.json()
            assert reconnect_state["events_since"] == []
            assert {output["output_id"] for output in reconnect_state["outputs"]} == {
                item["output_id"] for item in workflows
            }
