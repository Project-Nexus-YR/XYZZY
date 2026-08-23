"""End-to-end coverage for the conversation endpoints.

Threaded replies, derived counts, mention derivation on the write path, reactions,
the durable read cursor, the sequence cursor on the room listing, and search.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from fastapi.testclient import TestClient

from multiplayer.server import create_app

OWNER_HEADERS = {"Authorization": "Bearer owner-token"}
PEER_HEADERS = {"Authorization": "Bearer peer-token"}


def _seed(client: TestClient) -> dict[str, str]:
    org = client.post(
        "/api/v1/organizations", headers=OWNER_HEADERS, json={"name": "Conv", "slug": "conv"}
    ).json()
    workspace = client.post(
        f"/api/v1/organizations/{org['org_id']}/workspaces",
        headers=OWNER_HEADERS,
        json={"name": "Main", "slug": "main"},
    ).json()
    room = client.post(
        f"/api/v1/workspaces/{workspace['workspace_id']}/rooms",
        headers=OWNER_HEADERS,
        json={"name": "Authentication migration"},
    ).json()
    invited = client.post(
        f"/api/v1/rooms/{room['room_id']}/members/invitations",
        headers=OWNER_HEADERS,
        json={"user_id": "user-b", "role": "editor"},
    )
    assert invited.status_code == 200, invited.text
    templates = client.get("/api/v1/agent-templates", headers=OWNER_HEADERS).json()
    agent = client.post(
        f"/api/v1/rooms/{room['room_id']}/agents",
        headers=OWNER_HEADERS,
        json={"template_id": templates[0]["template_id"], "name": "Architect"},
    ).json()
    return {"room_id": room["room_id"], "agent_id": agent["agent_id"]}


def _app() -> TestClient:
    return TestClient(
        create_app(
            ":memory:",
            auth_tokens={"owner-token": "user-a", "peer-token": "user-b"},
        )
    )


def test_threads_reactions_read_state_and_search_end_to_end() -> None:
    with _app() as client:
        seeded = _seed(client)
        room_id = seeded["room_id"]

        root = client.post(
            f"/api/v1/rooms/{room_id}/messages",
            headers=OWNER_HEADERS,
            json={"content": "Do we adopt a managed identity provider?"},
        ).json()
        first = client.post(
            f"/api/v1/messages/{root['message_id']}/replies",
            headers=PEER_HEADERS,
            json={"content": "Only with a rollback rehearsal."},
        )
        assert first.status_code == 200, first.text
        second = client.post(
            f"/api/v1/messages/{first.json()['message_id']}/replies",
            headers=OWNER_HEADERS,
            json={"content": "Rehearsal needs a dual-write window."},
        ).json()

        assert first.json()["thread_depth"] == 1
        assert second["thread_depth"] == 2
        assert second["root_message_id"] == root["message_id"]

        thread = client.get(
            f"/api/v1/messages/{root['message_id']}/thread", headers=OWNER_HEADERS
        ).json()
        assert [entry["reply_count"] for entry in thread] == [1, 1, 0]

        # Reactions: add, list, remove, and re-add over one durable row.
        emoji = quote("👍")
        added = client.post(
            f"/api/v1/messages/{root['message_id']}/reactions",
            headers=PEER_HEADERS,
            json={"emoji": "👍"},
        )
        assert added.status_code == 200, added.text
        live = client.get(
            f"/api/v1/messages/{root['message_id']}/reactions", headers=OWNER_HEADERS
        ).json()
        assert [r["actor_id"] for r in live] == ["user-b"]
        removed = client.delete(
            f"/api/v1/messages/{root['message_id']}/reactions/{emoji}", headers=PEER_HEADERS
        )
        assert removed.status_code == 200, removed.text
        assert (
            client.get(
                f"/api/v1/messages/{root['message_id']}/reactions", headers=OWNER_HEADERS
            ).json()
            == []
        )
        client.post(
            f"/api/v1/messages/{root['message_id']}/reactions",
            headers=PEER_HEADERS,
            json={"emoji": "👍"},
        )
        assert (
            len(
                client.get(
                    f"/api/v1/messages/{root['message_id']}/reactions", headers=OWNER_HEADERS
                ).json()
            )
            == 1
        )

        # The room listing resumes from a sequence cursor, and it is the flat
        # channel: neither reply above asked to be broadcast, so neither appears.
        after = client.get(
            f"/api/v1/rooms/{room_id}/messages",
            headers=OWNER_HEADERS,
            params={"after_sequence": root["sequence"]},
        ).json()
        assert after == []

        # The read cursor is durable server-side state.
        before = client.get(f"/api/v1/rooms/{room_id}/read-cursor", headers=PEER_HEADERS).json()
        assert before["last_read_sequence"] == 0
        # One, not three: the listing directly above is the channel this reader is
        # shown, and neither reply was broadcast into it.
        assert before["unread_messages"] == 1
        client.put(
            f"/api/v1/rooms/{room_id}/read-cursor",
            headers=PEER_HEADERS,
            json={"last_read_sequence": second["sequence"]},
        )
        after_set = client.get(f"/api/v1/rooms/{room_id}/read-cursor", headers=PEER_HEADERS).json()
        assert after_set["last_read_sequence"] == second["sequence"]
        assert after_set["unread_messages"] == 0
        # It is per user: the owner's own position is untouched.
        assert (
            client.get(f"/api/v1/rooms/{room_id}/read-cursor", headers=OWNER_HEADERS).json()[
                "last_read_sequence"
            ]
            == 0
        )

        # A reply that explicitly asks for the channel does appear in the flat log.
        shared = client.post(
            f"/api/v1/messages/{root['message_id']}/replies",
            headers=PEER_HEADERS,
            json={"content": "Summary for the channel.", "broadcast_to_room": True},
        ).json()
        after_broadcast = client.get(
            f"/api/v1/rooms/{room_id}/messages",
            headers=OWNER_HEADERS,
            params={"after_sequence": root["sequence"]},
        ).json()
        assert [m["message_id"] for m in after_broadcast] == [shared["message_id"]]

        hits = client.get(
            "/api/v1/search", headers=OWNER_HEADERS, params={"q": "dual-write window"}
        ).json()
        assert [hit["object_id"] for hit in hits] == [second["message_id"]]
        # A hit names the channel it lives in, not only its id.
        assert hits[0]["room_name"] == "Authentication migration"


def test_a_mention_records_the_target_and_only_invokes_when_asked() -> None:
    with _app() as client:
        seeded = _seed(client)
        room_id, agent_id = seeded["room_id"], seeded["agent_id"]

        quiet = client.post(
            f"/api/v1/rooms/{room_id}/messages",
            headers=OWNER_HEADERS,
            json={"content": "@user-b and @Architect, opinions?"},
        ).json()
        assert {m["target_type"] for m in quiet["mentions"]} == {"USER", "AGENT"}
        assert all(m["invoked_execution_id"] is None for m in quiet["mentions"])

        invoked = client.post(
            f"/api/v1/rooms/{room_id}/messages",
            headers=OWNER_HEADERS,
            json={"content": "@Architect assess it now", "invoke_mentioned_agents": True},
        ).json()
        agent_mention = next(m for m in invoked["mentions"] if m["target_type"] == "AGENT")
        assert agent_mention["target_id"] == agent_id
        assert agent_mention["invoked_execution_id"]

        started = [
            event
            for event in client.get(f"/api/v1/rooms/{room_id}/events", headers=OWNER_HEADERS).json()
            if event["event_type"] == "agent.run.started"
        ]
        assert len(started) == 1
        assert started[0]["payload"]["triggered_by"] == "MENTION"

        # Why the agent spoke is readable from the run itself, not only the event.
        state = client.get(f"/api/v1/rooms/{room_id}/state", headers=OWNER_HEADERS).json()
        run = next(
            item
            for item in state["runs"]
            if item["execution_id"] == agent_mention["invoked_execution_id"]
        )
        assert run["triggered_by"] == "MENTION"
        assert run["status"] == "COMPLETED"
        produced = next(
            item for item in state["outputs"] if item["execution_id"] == run["execution_id"]
        )
        branch = client.get(
            f"/api/v1/branches/{produced['branch_id']}", headers=OWNER_HEADERS
        ).json()
        assert [item["triggered_by"] for item in branch["runs"]] == ["MENTION"]

        # The answer landed in the thread as an AGENT message pointing at its output.
        thread = client.get(
            f"/api/v1/messages/{invoked['message_id']}/thread", headers=OWNER_HEADERS
        ).json()
        answer = thread[-1]
        assert answer["role"] == "AGENT"
        assert answer["sender_id"] == agent_id
        assert answer["parent_message_id"] == invoked["message_id"]
        output_id = answer["metadata"]["output_id"]
        assert any(item["output_id"] == output_id for item in state["outputs"])

        # And an authenticated principal still cannot author one themselves.
        spoofed = client.post(
            f"/api/v1/rooms/{room_id}/messages",
            headers=OWNER_HEADERS,
            json={"content": "I am the agent", "role": "AGENT"},
        )
        assert spoofed.status_code == 403


def _channel_pane(client: TestClient, room_id: str) -> list[dict[str, object]]:
    """What the channel pane draws: the room snapshot's own message rows."""
    state = client.get(f"/api/v1/rooms/{room_id}/state", headers=OWNER_HEADERS).json()
    return list(state["messages"])


def test_an_answer_is_attributed_in_the_thread_and_broadcast_where_it_was_asked() -> None:
    """The thread rows are the only place a thread-scoped answer is read.

    An answer inherits the mention's broadcast, which is the rule that puts it where
    the question was: a mention typed in the channel broadcasts, so its answer is in
    the channel log too; a mention typed inside a thread does not, so its answer
    stays in the thread. Either way the row carries who spoke, on what trigger, at
    whose asking, and which output it is the surface of.
    """
    with _app() as client:
        seeded = _seed(client)
        room_id, agent_id = seeded["room_id"], seeded["agent_id"]

        asked_aloud = client.post(
            f"/api/v1/rooms/{room_id}/messages",
            headers=OWNER_HEADERS,
            json={"content": "@Architect assess it now", "invoke_mentioned_agents": True},
        ).json()
        thread = client.get(
            f"/api/v1/messages/{asked_aloud['message_id']}/thread", headers=OWNER_HEADERS
        ).json()
        answer = next(e for e in thread if e["parent_message_id"] == asked_aloud["message_id"])

        assert answer["role"] == "AGENT"
        assert answer["sender_id"] == agent_id
        assert answer["metadata"]["triggered_by"] == "MENTION"
        assert answer["metadata"]["requested_by"] == "user-a"
        assert answer["metadata"]["output_id"]

        # Asked in the channel, so answered in the channel as well as in the thread.
        assert answer["broadcast_to_room"] is True
        channel = _channel_pane(client, room_id)
        assert answer["message_id"] in [m["message_id"] for m in channel]

        asked_quietly = client.post(
            f"/api/v1/messages/{asked_aloud['message_id']}/replies",
            headers=OWNER_HEADERS,
            json={"content": "@Architect once more please", "invoke_mentioned_agents": True},
        ).json()
        assert asked_quietly["broadcast_to_room"] is False
        thread = client.get(
            f"/api/v1/messages/{asked_quietly['message_id']}/thread", headers=OWNER_HEADERS
        ).json()
        quiet = next(e for e in thread if e["parent_message_id"] == asked_quietly["message_id"])

        # Asked in the thread, so answered only there — with the same attribution.
        assert quiet["role"] == "AGENT"
        assert quiet["sender_id"] == agent_id
        assert quiet["metadata"]["triggered_by"] == "MENTION"
        assert quiet["broadcast_to_room"] is False
        channel = _channel_pane(client, room_id)
        assert quiet["message_id"] not in [m["message_id"] for m in channel]
        # The channel still says the thread moved, so nobody has to be watching it.
        root = next(m for m in channel if m["message_id"] == asked_aloud["message_id"])
        assert root["reply_count"] >= 3
        assert root["last_reply_at"]


def test_both_panes_attribute_an_agent_answer_through_one_renderer() -> None:
    """A pane with a template of its own is a pane that drifts, and the thread one had.

    It rendered an agent answer as the bare sender id with no trigger, no invoker
    and no way through to the output, while the channel pane beside it showed all
    four from the same fields.
    """
    ui = (Path(__file__).parents[2] / "web" / "index.html").read_text(encoding="utf-8")

    assert ui.count("function attribution(") == 1
    assert "msg.innerHTML = `${attribution(m)}" in ui
    assert "${attribution(entry," in ui
    # Both panes reach the roster name and the provenance line only through it, so
    # neither can lose a field the other keeps.
    assert ui.count("displayNameFor(") == 2
    assert ui.count("agentProvenance(") == 2
    assert "entry.sender_id === userId ? userName : entry.sender_id" not in ui
    # And the output record opens beside whichever row was clicked, thread included.
    assert "openAgentOutput(this.dataset.outputId, this)" in ui
    assert "trigger.closest('.msg, .thread-item')" in ui


def test_a_search_query_of_only_punctuation_is_rejected() -> None:
    with _app() as client:
        _seed(client)
        rejected = client.get("/api/v1/search", headers=OWNER_HEADERS, params={"q": "***"})
        assert rejected.status_code == 400


def test_an_unrecognized_handle_comes_back_named() -> None:
    """A misspelled handle used to return 200 with an empty mention list and no hint."""
    with _app() as client:
        seeded = _seed(client)

        sent = client.post(
            f"/api/v1/rooms/{seeded['room_id']}/messages",
            headers=OWNER_HEADERS,
            json={"content": "@architect and @architekt, plus @nobody"},
        )
        assert sent.status_code == 200, sent.text
        body = sent.json()

        assert [m["target_id"] for m in body["mentions"]] == [seeded["agent_id"]]
        assert body["unrecognized_mentions"] == ["architekt", "nobody"]


def test_the_roster_publishes_the_handle_people_have_to_type() -> None:
    with _app() as client:
        seeded = _seed(client)
        templates = client.get("/api/v1/agent-templates", headers=OWNER_HEADERS).json()
        reviewer = next(t for t in templates if t["name"] == "Security Reviewer")
        client.post(
            f"/api/v1/rooms/{seeded['room_id']}/agents",
            headers=OWNER_HEADERS,
            json={"template_id": reviewer["template_id"]},
        )

        state = client.get(f"/api/v1/rooms/{seeded['room_id']}/state", headers=OWNER_HEADERS)
        assert state.status_code == 200, state.text
        body = state.json()

        assert {a["name"]: a["handle"] for a in body["agents"]} == {
            "Architect": "architect",
            "Security Reviewer": "security-reviewer",
        }
        assert {m["user_id"]: m["handle"] for m in body["members"]} == {
            "user-a": "user-a",
            "user-b": "user-b",
        }


def test_an_http_principal_cannot_react_as_an_agent() -> None:
    """The route has no actor to override: a reaction is signed by the caller alone."""
    with _app() as client:
        seeded = _seed(client)
        room_id = seeded["room_id"]
        message = client.post(
            f"/api/v1/rooms/{room_id}/messages",
            headers=OWNER_HEADERS,
            json={"content": "Do we adopt a managed identity provider?"},
        ).json()

        reacted = client.post(
            f"/api/v1/messages/{message['message_id']}/reactions",
            headers=OWNER_HEADERS,
            json={
                "emoji": "\U0001f440",
                "actor_id": seeded["agent_id"],
                "actor_type": "AGENT",
            },
        )
        assert reacted.status_code == 200, reacted.text

        listed = client.get(
            f"/api/v1/messages/{message['message_id']}/reactions", headers=OWNER_HEADERS
        ).json()
        assert [(r["actor_id"], r["actor_type"]) for r in listed] == [("user-a", "USER")]
