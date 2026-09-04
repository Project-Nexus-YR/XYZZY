"""Object-addressed routes resolve authorization through their owning room."""

import re
import types
import typing
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.datastructures import UploadFile
from fastapi.testclient import TestClient
from pydantic import BaseModel

from multiplayer.api import routes
from multiplayer.server import create_app

ADMIN = {"Authorization": "Bearer admin-token"}
EDITOR = {"Authorization": "Bearer editor-token"}
VIEWER = {"Authorization": "Bearer viewer-token"}
OUTSIDER = {"Authorization": "Bearer outsider-token"}


def _client() -> Iterator[TestClient]:
    app = create_app(
        ":memory:",
        auth_tokens={
            "admin-token": "admin-user",
            "editor-token": "editor-user",
            "viewer-token": "viewer-user",
            "outsider-token": "outsider-user",
        },
    )
    with TestClient(app) as client:
        yield client


@pytest.fixture
def client() -> Iterator[TestClient]:
    yield from _client()


def _seed(client: TestClient) -> dict[str, str]:
    org = client.post(
        "/api/v1/organizations",
        headers=ADMIN,
        json={"name": "Security org", "slug": "security-org"},
    ).json()
    workspace = client.post(
        f"/api/v1/organizations/{org['org_id']}/workspaces",
        headers=ADMIN,
        json={"name": "Security workspace", "slug": "security-workspace"},
    ).json()
    room = client.post(
        f"/api/v1/workspaces/{workspace['workspace_id']}/rooms",
        headers=ADMIN,
        json={"name": "Private room"},
    ).json()
    room_id = room["room_id"]
    for user_id, role in (("editor-user", "editor"), ("viewer-user", "viewer")):
        response = client.post(
            f"/api/v1/rooms/{room_id}/members/invitations",
            headers=ADMIN,
            json={"user_id": user_id, "role": role},
        )
        assert response.status_code == 200

    templates = client.get("/api/v1/agent-templates", headers=ADMIN).json()
    agent = client.post(
        f"/api/v1/rooms/{room_id}/agents",
        headers=ADMIN,
        json={"template_id": templates[0]["template_id"]},
    ).json()
    session = client.post(
        f"/api/v1/rooms/{room_id}/agents/{agent['agent_id']}/sessions",
        headers=ADMIN,
    ).json()
    execution = client.post(
        f"/api/v1/sessions/{session['session_id']}/execute",
        headers=ADMIN,
    ).json()
    task = client.post(
        f"/api/v1/rooms/{room_id}/tasks",
        headers=ADMIN,
        json={"title": "Protected task"},
    ).json()
    artifact = client.post(
        f"/api/v1/rooms/{room_id}/artifacts",
        headers=ADMIN,
        json={"name": "Protected artifact", "content": "version one"},
    ).json()
    version = client.post(
        f"/api/v1/artifacts/{artifact['artifact_id']}/versions",
        headers=ADMIN,
        json={"content": "version two"},
    ).json()
    approval = client.post(
        f"/api/v1/rooms/{room_id}/approvals",
        headers=ADMIN,
        params={
            "execution_id": execution["execution_id"],
            "agent_id": agent["agent_id"],
            "action": "Publish migration",
        },
    ).json()
    return {
        "room_id": room_id,
        "agent_id": agent["agent_id"],
        "execution_id": execution["execution_id"],
        "task_id": task["task_id"],
        "artifact_id": artifact["artifact_id"],
        "version_id": version["version_id"],
        "approval_id": approval["approval_id"],
    }


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_approval_decision_requires_admin_and_denials_have_no_side_effects(
    client: TestClient, decision: str
) -> None:
    seeded = _seed(client)
    room_id = seeded["room_id"]
    path = f"/api/v1/approvals/{seeded['approval_id']}/{decision}"
    state_before = client.get(f"/api/v1/rooms/{room_id}/state", headers=ADMIN).json()

    attempts = ((None, 401), (OUTSIDER, 403), (VIEWER, 403), (EDITOR, 403))
    for headers, expected_status in attempts:
        response = client.post(path, headers=headers, json={"comment": "spoofed"})
        assert response.status_code == expected_status
        state_after = client.get(f"/api/v1/rooms/{room_id}/state", headers=ADMIN).json()
        assert state_after == state_before
        pending = client.get(f"/api/v1/rooms/{room_id}/approvals", headers=ADMIN).json()
        assert pending == [
            {
                "approval_id": seeded["approval_id"],
                "action": "Publish migration",
                "agent_id": seeded["agent_id"],
                "status": "PENDING",
            }
        ]


@pytest.mark.parametrize(
    ("decision", "event_type"),
    (("approve", "approval.granted"), ("reject", "approval.rejected")),
)
def test_admin_approval_actor_comes_only_from_bearer_principal(
    client: TestClient, decision: str, event_type: str
) -> None:
    seeded = _seed(client)
    response = client.post(
        f"/api/v1/approvals/{seeded['approval_id']}/{decision}",
        headers=ADMIN,
        params={"user_id": "spoofed-reviewer"},
        json={"comment": "reviewed"},
    )
    assert response.status_code == 200

    events = client.get(f"/api/v1/rooms/{seeded['room_id']}/events", headers=ADMIN).json()
    decision_events = [event for event in events if event["event_type"] == event_type]
    assert len(decision_events) == 1
    assert decision_events[0]["actor_id"] == "admin-user"
    assert decision_events[0]["actor_type"] == "user"
    assert decision_events[0]["payload"]["reviewer_id"] == "admin-user"


def test_entity_routes_authorize_through_owning_room_before_mutation(
    client: TestClient,
) -> None:
    seeded = _seed(client)
    room_id = seeded["room_id"]
    state_before = client.get(f"/api/v1/rooms/{room_id}/state", headers=ADMIN).json()
    mutations: list[tuple[str, dict[str, Any]]] = [
        (
            f"/api/v1/tasks/{seeded['task_id']}/assign",
            {"agent_id": seeded["agent_id"]},
        ),
        (
            f"/api/v1/tasks/{seeded['task_id']}/delegate",
            {"to_agent_id": seeded["agent_id"]},
        ),
        (f"/api/v1/tasks/{seeded['task_id']}/complete", {}),
        (f"/api/v1/tasks/{seeded['task_id']}/cancel", {}),
        (
            f"/api/v1/artifacts/{seeded['artifact_id']}/versions",
            {"content": "unauthorized version"},
        ),
        (f"/api/v1/agents/{seeded['agent_id']}/interrupt", {"reason": "stop"}),
        (
            f"/api/v1/agents/{seeded['agent_id']}/redirect",
            {"instruction": "exfiltrate"},
        ),
    ]
    for headers in (OUTSIDER, VIEWER):
        for path, body in mutations:
            response = client.post(path, headers=headers, json=body)
            assert response.status_code == 403, path

    protected_reads = (
        f"/api/v1/artifacts/{seeded['artifact_id']}/versions",
        f"/api/v1/artifact-versions/{seeded['version_id']}/provenance",
    )
    for path in protected_reads:
        assert client.get(path, headers=OUTSIDER).status_code == 403
        assert client.get(path, headers=VIEWER).status_code == 200

    state_after = client.get(f"/api/v1/rooms/{room_id}/state", headers=ADMIN).json()
    assert state_after == state_before


def test_approval_request_rejects_cross_room_execution_and_agent(
    client: TestClient,
) -> None:
    first = _seed(client)
    workspace_id = client.get(f"/api/v1/rooms/{first['room_id']}", headers=ADMIN).json()[
        "workspace_id"
    ]
    second_room = client.post(
        f"/api/v1/workspaces/{workspace_id}/rooms",
        headers=ADMIN,
        json={"name": "Second private room"},
    ).json()
    second_room_id = second_room["room_id"]
    state_before = client.get(f"/api/v1/rooms/{second_room_id}/state", headers=ADMIN).json()

    response = client.post(
        f"/api/v1/rooms/{second_room_id}/approvals",
        headers=ADMIN,
        params={
            "execution_id": first["execution_id"],
            "agent_id": first["agent_id"],
            "action": "Cross-room mutation",
        },
    )
    assert response.status_code == 400
    assert client.get(f"/api/v1/rooms/{second_room_id}/state", headers=ADMIN).json() == state_before
    pending = client.get(f"/api/v1/rooms/{second_room_id}/approvals", headers=ADMIN).json()
    assert pending == []


def _all_api_routes(app: Any) -> list[Any]:
    """Every route FastAPI actually serves, walked past whatever wraps a
    sub-router on this FastAPI version, rather than a list somebody typed by
    hand and never revisits when a route is added.
    """
    flat: list[Any] = []
    for route in app.routes:
        if hasattr(route, "original_router"):
            flat.extend(_all_api_routes(route.original_router))
        elif hasattr(route, "routes"):
            flat.extend(_all_api_routes(route))
        else:
            flat.append(route)
    return flat


# Self-scoped (act on the caller's own identity or session, nothing to own a
# room) or pre-auth (answer before, or without needing, a credential). Every
# one of these has its own dedicated authorization test elsewhere, or, like
# `/api/v1/search`, states in its own docstring why a Python-level check would
# only hide the thing that actually enforces isolation.
SELF_SCOPED_OR_PRE_AUTH_PATHS = frozenset(
    {
        "/",
        "/docs",
        "/docs/oauth2-redirect",
        "/redoc",
        "/openapi.json",
        "/metrics",
        "/share/{token}",
        "/.well-known/agent-card.json",
        "/a2a/v1",  # JSON-RPC: every call is HTTP 200 with an envelope, per spec.
        "/api/v1/health",
        "/api/v1/search",
        "/api/v1/auth/login",
        "/api/v1/auth/callback",
        "/api/v1/auth/config",
        "/api/v1/auth/refresh",
        "/api/v1/auth/logout",
        "/api/v1/auth/logout-everywhere",
        "/api/v1/auth/end-session",
        "/api/v1/auth/backchannel-logout",
        "/api/v1/auth/frontchannel-logout",
        "/api/v1/me/bootstrap",
        "/api/v1/me/context",
        "/api/v1/notifications",
        "/api/v1/organizations",  # creating one's own org, nothing yet to own it
        "/api/v1/agent-templates",  # the built-in catalog, not room- or workspace-scoped
    }
)


def _dummy_value(annotation: Any) -> Any:
    """One syntactically valid value per field type, so a request body passes
    pydantic parsing rather than failing it: this sweep does not care whether
    the *value* is a domain-valid one, only that the outsider's request gets
    far enough to reach an authorization check instead of failing at 422
    before that check ever runs."""
    origin = typing.get_origin(annotation)
    if origin in (typing.Union, types.UnionType):
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        return _dummy_value(args[0]) if args else None
    if origin is list:
        return []
    if origin is dict:
        return {}
    if annotation is str:
        return "x"
    if annotation is int:
        return 1
    if annotation is float:
        return 1.0
    if annotation is bool:
        return True
    return "x"


def _example_request_kwargs(route: Any) -> dict[str, Any]:
    """A minimally valid body for a route's own declared request model, built
    from the model's own required fields rather than a hand-typed dict per
    path that a new field would silently outgrow. A multipart upload route
    gets a small file instead of JSON."""
    body_field = getattr(route, "body_field", None)
    if body_field is None:
        return {}
    model_cls = body_field.field_info.annotation
    if not (isinstance(model_cls, type) and issubclass(model_cls, BaseModel)):
        return {}
    upload_fields = [
        name for name, field in model_cls.model_fields.items() if field.annotation is UploadFile
    ]
    if upload_fields:
        return {"files": {name: ("proof.txt", b"proof", "text/plain") for name in upload_fields}}
    data = {
        name: _dummy_value(field.annotation)
        for name, field in model_cls.model_fields.items()
        if field.is_required()
    }
    return {"json": data}


def test_every_route_refuses_an_authenticated_outsider_a_200(client: TestClient) -> None:
    """A structural guard over the route table itself, not a hand-maintained
    list of paths (finding 21): a new route reaching this app without its own
    authorization call fails this test by existing, rather than by someone
    remembering to add it here.

    An outsider who belongs to no organization, workspace, or room anywhere
    calls every route this app serves with a placeholder id in each path
    parameter, and, for a body-taking route, a minimally valid body built
    from the route's own request model (finding 28): a route that validated
    the body before checking authorization used to escape this sweep behind
    a 422, which is not a refusal. 401, 403 and 404 are all legitimate ways
    to refuse a stranger; 200, 422, and 5xx are not.
    """
    full_app = create_app(
        ":memory:",
        auth_tokens={"admin-token": "admin-user", "outsider-token": "outsider-user"},
    )
    checked = 0
    with TestClient(full_app) as outsider_client:
        for route in _all_api_routes(full_app):
            path = getattr(route, "path", None)
            methods = getattr(route, "methods", None)
            if not path or not methods or path in SELF_SCOPED_OR_PRE_AUTH_PATHS:
                continue
            placeholder_path = re.sub(r"\{[^}]+\}", "does-not-exist", path)
            for method in sorted(methods - {"HEAD", "OPTIONS"}):
                checked += 1
                request_kwargs: dict[str, Any] = {}
                if method in {"POST", "PUT", "PATCH"}:
                    request_kwargs = _example_request_kwargs(route)
                response = outsider_client.request(
                    method,
                    placeholder_path,
                    headers=OUTSIDER,
                    **request_kwargs,
                )
                assert response.status_code in {401, 403, 404}, (
                    method,
                    path,
                    response.status_code,
                    response.text,
                )
    assert checked > 50  # the sweep itself must not have silently found nothing


def test_the_outsider_sweep_would_catch_an_unguarded_body_taking_route() -> None:
    """Proof that the assertion above is not itself vacuous: a body-taking
    write route with no authorization call at all really does answer 200 to
    a stranger, given a minimally valid body, rather than 422. If the
    production sweep's own bodies were somehow still short of what a route
    needed, this reproduction would answer 422 too, which would mean the
    sweep's 401/403/404 assertion could never distinguish "authorized" from
    "body invalid", and it would fail right here, on this synthetic route,
    long before it got a chance to fail silently on a real one.
    """

    class _UnguardedBody(BaseModel):
        title: str

    probe_app = FastAPI()

    @probe_app.post("/probe/{thing_id}")
    async def _unguarded(thing_id: str, payload: _UnguardedBody) -> dict[str, str]:
        return {"thing_id": thing_id, "title": payload.title}

    with TestClient(probe_app) as probe_client:
        response = probe_client.post("/probe/does-not-exist", json={"title": "x"})
    assert response.status_code == 200, response.text


def test_notification_query_cannot_override_bearer_identity(client: TestClient) -> None:
    observed_user_ids: list[str] = []
    service = routes._svc
    original = service.list_notifications

    async def capture_user(user_id: str) -> list[Any]:
        observed_user_ids.append(user_id)
        return []

    service.list_notifications = capture_user
    try:
        anonymous = client.get("/api/v1/notifications?user_id=admin-user")
        assert anonymous.status_code == 401
        outsider = client.get("/api/v1/notifications?user_id=admin-user", headers=OUTSIDER)
        assert outsider.status_code == 200
        admin = client.get("/api/v1/notifications?user_id=outsider-user", headers=ADMIN)
        assert admin.status_code == 200
    finally:
        service.list_notifications = original

    assert observed_user_ids == ["outsider-user", "admin-user"]
