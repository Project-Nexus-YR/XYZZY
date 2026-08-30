"""Browser sign-in over an HttpOnly cookie.

The refresh token never reaches the browser: only the callback's HTML branch
sets a cookie, and it carries the access token alone. Everything that makes a
cookie usable as a credential is asserted here — the config probe the client
checks first, the header gate that keeps a mutating GET out of CSRF reach, the
Origin allowlist WebSocket cookie auth uses instead, and that logout actually
clears the cookie it set.
"""

from __future__ import annotations

import os

import httpx
import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from starlette.websockets import WebSocketDisconnect

import multiplayer.realtime.websocket as websocket_module
from multiplayer.api import routes
from multiplayer.security.oidc import OidcProvider, OidcSettings
from multiplayer.security.sessions import SessionService
from multiplayer.server import create_app

from ..security.test_sso_session_lifecycle import CLIENT_ID, ISSUER, FakeProvider

TOKENS = {"owner-token": "user_1"}


def _configured(
    idp: FakeProvider, *, https: bool = True, host: str = "xyzzy.example"
) -> SessionService:
    """Point a configured session service at the running app's own database.

    ``host`` must match the test client's own host whenever ``https`` matches
    the client's scheme too, or `/auth/login`'s new entry-host alignment
    redirect fires (correctly) and the flow tested here never reaches the
    provider. Callers on `AsyncClient(base_url="http://test")` in http mode,
    or on `TestClient`'s default `http://testserver` in http mode, must pass
    the matching host explicitly; the default is intentionally a mismatch for
    https-mode callers, where the differing scheme already exempts them.
    """
    svc = routes._svc
    assert svc is not None
    scheme = "https" if https else "http"
    sessions = SessionService(
        db=svc.db,
        repos=svc.repos,
        provider=OidcProvider(
            settings=OidcSettings(
                issuer=ISSUER,
                client_id=CLIENT_ID,
                client_secret="shh",
                redirect_uri=f"{scheme}://{host}/callback",
            )
        ),
    )
    transport = httpx.MockTransport(idp.handler)
    sessions._client = lambda: httpx.AsyncClient(transport=transport)  # type: ignore[method-assign]
    return sessions


async def _sign_in_by_cookie(client: AsyncClient, idp: FakeProvider) -> str:
    """Drive the whole browser handoff over HTTP and return the session cookie."""
    login = await client.get("/api/v1/auth/login")
    params = httpx.URL(login.headers["location"]).params
    idp.expect_nonce = params["nonce"]
    login_binding = login.cookies.get("xyzzy_login") or login.cookies.get("__Host-xyzzy_login")

    callback = await client.get(
        "/api/v1/auth/callback",
        params={"state": params["state"], "code": "auth-code"},
        headers={"accept": "text/html,application/xhtml+xml"},
        cookies={"xyzzy_login": login_binding, "__Host-xyzzy_login": login_binding},
    )
    assert callback.status_code == 200, callback.text
    session_cookie = callback.cookies.get("__Host-xyzzy_session") or callback.cookies.get(
        "xyzzy_session"
    )
    assert session_cookie
    return session_cookie


@pytest.mark.asyncio
async def test_auth_config_reports_sso_state():
    app = create_app(":memory:", auth_tokens=TOKENS)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            without_provider = await client.get("/api/v1/auth/config")
            assert without_provider.status_code == 200
            assert without_provider.json() == {
                "sso": False,
                "provider_label": "single sign-on",
                "authenticated": False,
            }

            os.environ["XYZZY_OIDC_PROVIDER_LABEL"] = "Acme SSO"
            try:
                routes.set_sessions(_configured(FakeProvider()))
                with_provider = await client.get("/api/v1/auth/config")
                # A live session cookie flips authenticated without any header,
                # and a bogus one does not; neither case needs a 401 in the
                # browser console just to find out.
                bogus = await client.get(
                    "/api/v1/auth/config", cookies={"xyzzy_session": "not-a-token"}
                )
            finally:
                del os.environ["XYZZY_OIDC_PROVIDER_LABEL"]
            assert with_provider.json() == {
                "sso": True,
                "provider_label": "Acme SSO",
                "authenticated": False,
            }
            assert bogus.json()["authenticated"] is False


@pytest.mark.asyncio
async def test_html_callback_sets_cookie_and_json_callback_stays_byte_compatible():
    app = create_app(":memory:", auth_tokens=TOKENS)
    idp = FakeProvider()
    async with app.router.lifespan_context(app):
        routes.set_sessions(_configured(idp))
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", follow_redirects=False
        ) as client:
            login = await client.get("/api/v1/auth/login")
            params = httpx.URL(login.headers["location"]).params
            idp.expect_nonce = params["nonce"]
            login_binding = login.cookies["__Host-xyzzy_login"]

            html_callback = await client.get(
                "/api/v1/auth/callback",
                params={"state": params["state"], "code": "auth-code"},
                headers={"accept": "text/html,application/xhtml+xml"},
                cookies={"__Host-xyzzy_login": login_binding},
            )

            # The cookie the callback just minted flips the unauthenticated
            # config probe to authenticated - the client's silent session check.
            config = await client.get(
                "/api/v1/auth/config",
                cookies={"__Host-xyzzy_session": html_callback.cookies["__Host-xyzzy_session"]},
            )
            assert config.json()["authenticated"] is True

    # A 200 page that navigates itself, not a 303: the consumed-state callback
    # URL must never become a history entry a Back press can re-GET.
    assert html_callback.status_code == 200
    assert html_callback.headers["content-type"].startswith("text/html")
    assert "location.replace('/')" in html_callback.text
    cookie_header = html_callback.headers["set-cookie"]
    assert "__Host-xyzzy_session=" in cookie_header
    assert "httponly" in cookie_header.lower()
    assert "samesite=lax" in cookie_header.lower()
    assert "secure" in cookie_header.lower()
    assert "path=/" in cookie_header.lower()
    # No token or other credential in the markup at all — the cookie already
    # rode the Set-Cookie header, which is the only place one belongs here.
    access_token = html_callback.cookies["__Host-xyzzy_session"]
    assert access_token not in html_callback.text
    # "refresh" alone also matches the no-JS <meta http-equiv="refresh"> tag;
    # what must be absent is the credential, not that word.
    assert "refresh_token" not in html_callback.text.lower()
    assert "access_token" not in html_callback.text.lower()

    # A non-HTML caller against a *fresh* attempt still gets today's JSON body,
    # unchanged: same keys, both credentials, no cookie.
    app2 = create_app(":memory:", auth_tokens=TOKENS)
    idp2 = FakeProvider()
    async with app2.router.lifespan_context(app2):
        routes.set_sessions(_configured(idp2))
        async with AsyncClient(
            transport=ASGITransport(app=app2), base_url="http://test", follow_redirects=False
        ) as client:
            login2 = await client.get("/api/v1/auth/login")
            params2 = httpx.URL(login2.headers["location"]).params
            idp2.expect_nonce = params2["nonce"]
            binding2 = login2.cookies["__Host-xyzzy_login"]
            json_callback = await client.get(
                "/api/v1/auth/callback",
                params={"state": params2["state"], "code": "auth-code"},
                headers={"accept": "application/json"},
                cookies={"__Host-xyzzy_login": binding2},
            )
    assert json_callback.status_code == 200
    body = json_callback.json()
    assert set(body) == {
        "access_token",
        "refresh_token",
        "token_type",
        "session_id",
        "user_id",
        "idle_expires_at",
        "absolute_expires_at",
    }
    # The login-binding cookie is still cleared (as it always was); no *session*
    # cookie is issued to a caller that did not ask for the browser handoff.
    assert "xyzzy_session" not in json_callback.headers.get("set-cookie", "")


@pytest.mark.asyncio
async def test_pressing_back_after_sign_in_gets_a_styled_page_not_raw_json():
    """The callback's state is consumed on first use. Re-GETting the exact same
    URL — what pressing Back and letting the browser replay it does — must land
    an HTML caller on the same styled page a failed login gets, not a raw JSON
    {"detail": ...} body. A non-HTML caller still gets that JSON body, unchanged.
    """
    app = create_app(":memory:", auth_tokens=TOKENS)
    idp = FakeProvider()
    async with app.router.lifespan_context(app):
        routes.set_sessions(_configured(idp))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            login = await client.get("/api/v1/auth/login")
            params = httpx.URL(login.headers["location"]).params
            idp.expect_nonce = params["nonce"]
            login_binding = login.cookies["__Host-xyzzy_login"]
            query = {"state": params["state"], "code": "auth-code"}
            cookies = {"__Host-xyzzy_login": login_binding}

            first = await client.get(
                "/api/v1/auth/callback",
                params=query,
                headers={"accept": "text/html"},
                cookies=cookies,
            )
            assert first.status_code == 200

            replay_html = await client.get(
                "/api/v1/auth/callback",
                params=query,
                headers={"accept": "text/html"},
                cookies=cookies,
            )
            replay_json = await client.get(
                "/api/v1/auth/callback",
                params=query,
                headers={"accept": "application/json"},
                cookies=cookies,
            )

    assert replay_html.status_code == 400
    assert replay_html.headers["content-type"].startswith("text/html")
    assert "This sign-in could not be completed." in replay_html.text
    assert '<a href="/">' in replay_html.text

    assert replay_json.status_code == 400
    assert replay_json.headers["content-type"].startswith("application/json")
    assert replay_json.json() == {"detail": "this sign-in could not be completed"}


@pytest.mark.asyncio
async def test_login_realigns_to_the_redirect_uri_host_before_minting_anything():
    """A user who opened the app on a host that differs from `redirect_uri`'s
    (localhost vs 127.0.0.1, say) would otherwise set the login-binding cookie
    on a host the callback never revisits, and the whole sign-in 400s.
    """
    app = create_app(":memory:", auth_tokens=TOKENS)
    idp = FakeProvider()
    async with app.router.lifespan_context(app):
        routes.set_sessions(_configured(idp, https=False, host="127.0.0.1"))
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://localhost", follow_redirects=False
        ) as mismatched:
            realigned = await mismatched.get("/api/v1/auth/login")
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://127.0.0.1", follow_redirects=False
        ) as matching:
            unaligned = await matching.get("/api/v1/auth/login")

    assert realigned.status_code == 307
    assert realigned.headers["location"] == "http://127.0.0.1/api/v1/auth/login"
    # A matching host goes straight to the provider, as it always did.
    assert unaligned.status_code == 307
    assert "idp.example" in unaligned.headers["location"]


@pytest.mark.asyncio
async def test_cookie_auth_needs_the_web_client_header():
    app = create_app(":memory:", auth_tokens=TOKENS)
    idp = FakeProvider()
    async with app.router.lifespan_context(app):
        routes.set_sessions(_configured(idp))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            session_cookie = await _sign_in_by_cookie(client, idp)

            refused = await client.get(
                "/api/v1/me/context", cookies={"__Host-xyzzy_session": session_cookie}
            )
            accepted = await client.get(
                "/api/v1/me/context",
                cookies={"__Host-xyzzy_session": session_cookie},
                headers={"X-XYZZY-Client": "web"},
            )

    assert refused.status_code == 401
    assert accepted.status_code == 200

    # Bearer calls are unaffected either way.
    app2 = create_app(":memory:", auth_tokens=TOKENS)
    async with app2.router.lifespan_context(app2):
        async with AsyncClient(transport=ASGITransport(app=app2), base_url="http://test") as client:
            bearer_no_header = await client.get(
                "/api/v1/me/context", headers={"Authorization": "Bearer owner-token"}
            )
            bearer_with_header = await client.get(
                "/api/v1/me/context",
                headers={"Authorization": "Bearer owner-token", "X-XYZZY-Client": "web"},
            )
    assert bearer_no_header.status_code == 200
    assert bearer_with_header.status_code == 200


@pytest.mark.asyncio
async def test_cookie_auth_accepts_only_the_cookie_name_its_own_scheme_sets():
    """A related-subdomain attacker (or a downgraded request) can plant the
    weaker plain-named cookie. On an HTTPS deployment only __Host-xyzzy_session
    may ever authenticate; on an HTTP deployment only the plain name can exist
    at all, so that one must still work.
    """
    app = create_app(":memory:", auth_tokens=TOKENS)
    idp = FakeProvider()
    async with app.router.lifespan_context(app):
        routes.set_sessions(_configured(idp, https=True))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            session_cookie = await _sign_in_by_cookie(client, idp)

            wrong_name = await client.get(
                "/api/v1/me/context",
                cookies={"xyzzy_session": session_cookie},
                headers={"X-XYZZY-Client": "web"},
            )
            right_name = await client.get(
                "/api/v1/me/context",
                cookies={"__Host-xyzzy_session": session_cookie},
                headers={"X-XYZZY-Client": "web"},
            )
    assert wrong_name.status_code == 401
    assert right_name.status_code == 200

    app2 = create_app(":memory:", auth_tokens=TOKENS)
    idp2 = FakeProvider()
    async with app2.router.lifespan_context(app2):
        routes.set_sessions(_configured(idp2, https=False, host="test"))
        async with AsyncClient(transport=ASGITransport(app=app2), base_url="http://test") as client:
            plain_session_cookie = await _sign_in_by_cookie(client, idp2)
            plain_accepted = await client.get(
                "/api/v1/me/context",
                cookies={"xyzzy_session": plain_session_cookie},
                headers={"X-XYZZY-Client": "web"},
            )
    assert plain_accepted.status_code == 200


@pytest.mark.asyncio
async def test_end_session_is_unreachable_by_a_bare_cookie():
    app = create_app(":memory:", auth_tokens=TOKENS)
    idp = FakeProvider()
    async with app.router.lifespan_context(app):
        routes.set_sessions(_configured(idp))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            session_cookie = await _sign_in_by_cookie(client, idp)

            bare = await client.get(
                "/api/v1/auth/end-session", cookies={"__Host-xyzzy_session": session_cookie}
            )
            with_header = await client.get(
                "/api/v1/auth/end-session",
                cookies={"__Host-xyzzy_session": session_cookie},
                headers={"X-XYZZY-Client": "web"},
            )

    assert bare.status_code == 401
    assert with_header.status_code == 200


@pytest.mark.asyncio
async def test_logout_by_cookie_deletes_the_cookie():
    app = create_app(":memory:", auth_tokens=TOKENS)
    idp = FakeProvider()
    async with app.router.lifespan_context(app):
        routes.set_sessions(_configured(idp))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            session_cookie = await _sign_in_by_cookie(client, idp)

            answer = await client.post(
                "/api/v1/auth/logout",
                cookies={"__Host-xyzzy_session": session_cookie},
                headers={"X-XYZZY-Client": "web"},
            )

    assert answer.status_code == 200
    assert answer.json()["ended"] is True
    deletion = answer.headers["set-cookie"]
    assert "__Host-xyzzy_session=" in deletion
    assert "max-age=0" in deletion.lower()


def test_cookie_authed_websocket_checks_origin(tmp_path):
    """Cookie WS auth: accepted from a configured origin, refused from a foreign one."""
    db_path = tmp_path / "ws_cookie.db"
    app = create_app(str(db_path), auth_tokens=TOKENS)
    idp = FakeProvider()
    with TestClient(app) as client:
        routes.set_sessions(_configured(idp, https=False, host="testserver"))
        login = client.get("/api/v1/auth/login", follow_redirects=False)
        params = httpx.URL(login.headers["location"]).params
        idp.expect_nonce = params["nonce"]
        login_binding = login.cookies["xyzzy_login"]

        callback = client.get(
            "/api/v1/auth/callback",
            params={"state": params["state"], "code": "auth-code"},
            headers={"accept": "text/html"},
            cookies={"xyzzy_login": login_binding},
            follow_redirects=False,
        )
        assert callback.status_code == 200
        session_cookie = callback.cookies["xyzzy_session"]
        client.cookies.set("xyzzy_session", session_cookie)

        bootstrap = client.post(
            "/api/v1/me/bootstrap",
            headers={"X-XYZZY-Client": "web"},
            json={"display_name": "Owner", "room_name": "Ops"},
        )
        assert bootstrap.status_code == 200, bootstrap.text
        room_id = bootstrap.json()["room"]["room_id"]

        with pytest.raises(WebSocketDisconnect) as refused:
            with client.websocket_connect(
                f"/ws?room_id={room_id}", headers={"origin": "http://evil.example"}
            ) as ws:
                ws.receive_json()
        assert refused.value.code == 4401

        with client.websocket_connect(
            f"/ws?room_id={room_id}", headers={"origin": "http://localhost:8000"}
        ) as ws:
            assert ws.receive_json()["type"] == "connected"


def test_revoking_a_cookie_authed_session_closes_the_open_socket(tmp_path, monkeypatch):
    monkeypatch.setattr(websocket_module, "REAUTH_SECONDS", 0.05)
    db_path = tmp_path / "ws_cookie_revoke.db"
    app = create_app(str(db_path), auth_tokens=TOKENS)
    idp = FakeProvider()
    with TestClient(app) as client:
        sessions = _configured(idp, https=False, host="testserver")
        routes.set_sessions(sessions)
        login = client.get("/api/v1/auth/login", follow_redirects=False)
        params = httpx.URL(login.headers["location"]).params
        idp.expect_nonce = params["nonce"]
        login_binding = login.cookies["xyzzy_login"]
        callback = client.get(
            "/api/v1/auth/callback",
            params={"state": params["state"], "code": "auth-code"},
            headers={"accept": "text/html"},
            cookies={"xyzzy_login": login_binding},
            follow_redirects=False,
        )
        session_cookie = callback.cookies["xyzzy_session"]
        client.cookies.set("xyzzy_session", session_cookie)

        bootstrap = client.post(
            "/api/v1/me/bootstrap",
            headers={"X-XYZZY-Client": "web"},
            json={"display_name": "Owner", "room_name": "Ops"},
        )
        room_id = bootstrap.json()["room"]["room_id"]

        with client.websocket_connect(
            f"/ws?room_id={room_id}", headers={"origin": "http://localhost:8000"}
        ) as ws:
            assert ws.receive_json()["type"] == "connected"

            import asyncio

            async def _revoke() -> None:
                svc = routes._svc
                assert svc is not None
                rows = await svc.db.fetch_all(
                    "SELECT session_id FROM user_sessions WHERE revoked_at IS NULL"
                )
                for row in rows:
                    await sessions.end_session(row["session_id"], "revoked in test")

            asyncio.run(_revoke())

            with pytest.raises(WebSocketDisconnect) as disconnect:
                for _ in range(100):
                    ws.receive_json()
            assert disconnect.value.code == 4401
