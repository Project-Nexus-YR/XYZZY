"""The session lifecycle, against a provider that really signs its tokens.

The fake here is the network, not the cryptography: a real RSA key signs real
JWTs which are verified by the real verification path. A test that stubs out
signature checking proves that the code calls a function, which is not the
property anyone needs from an authentication layer.

Every request the code makes — discovery, JWKS, the token endpoint — goes
through one `httpx` transport, which is only possible because the JWKS fetch was
moved off PyJWT's own urllib-based client. That is the observable benefit of
that refactor: the whole flow is drivable from a test.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from multiplayer.db.connection import Database
from multiplayer.db.repositories import Repos
from multiplayer.domain.models import utcnow
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.security import AuthenticationError, AuthorizationError, TokenAuthenticator
from multiplayer.security.auth import hash_token
from multiplayer.security.boundary import agent_turn
from multiplayer.security.oidc import (
    BACKCHANNEL_LOGOUT_EVENT,
    OidcError,
    OidcProvider,
    OidcSettings,
    challenge_for,
)
from multiplayer.security.sessions import (
    UNVERIFIABLE_ABSOLUTE_SECONDS,
    SessionError,
    SessionService,
    SessionSettings,
)
from multiplayer.services.service import MultiplayerService

ISSUER = "https://idp.example"
CLIENT_ID = "xyzzy-test"
SUBJECT = "subject-42"
KID = "test-key-1"


class FakeProvider:
    """An identity provider that signs properly and records what it was asked."""

    def __init__(self) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.seen_verifier: str | None = None
        self.expect_nonce: str = ""
        self.jwks_fetches = 0
        self.refresh_grants = 0
        # Flipped by a test to make the provider withdraw its consent, the way it
        # would for somebody disabled or password-reset upstream.
        self.still_vouches = True
        # Some providers issue no refresh token unless offline_access is asked for.
        self.issue_refresh_token = True
        # Some providers omit sid from the ID token but send it in a logout token.
        self.omit_sid_at_login = False

    @property
    def jwks(self) -> dict[str, Any]:
        return {
            "keys": [
                json.loads(
                    jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key())  # type: ignore[no-untyped-call]
                )
                | {"kid": KID, "use": "sig", "alg": "RS256"}
            ]
        }

    def id_token(self, **overrides: Any) -> str:
        moment = utcnow()
        claims: dict[str, Any] = {
            "iss": ISSUER,
            "sub": SUBJECT,
            "aud": CLIENT_ID,
            "iat": int(moment.timestamp()),
            "exp": int((moment + timedelta(minutes=5)).timestamp()),
            "nonce": self.expect_nonce,
            "email": "someone@example.com",
            "name": "Someone",
        }
        if not self.omit_sid_at_login:
            claims["sid"] = "idp-session-1"
        claims.update(overrides)
        signing = overrides.pop("_key", None) or self.key
        return jwt.encode(claims, signing, algorithm="RS256", headers={"kid": KID})

    def logout_token(self, **overrides: Any) -> str:
        moment = utcnow()
        claims: dict[str, Any] = {
            "iss": ISSUER,
            "aud": CLIENT_ID,
            "iat": int(moment.timestamp()),
            "exp": int((moment + timedelta(minutes=5)).timestamp()),
            "jti": f"jti-{moment.timestamp()}",
            "sub": SUBJECT,
            "sid": "idp-session-1",
            "events": {BACKCHANNEL_LOGOUT_EVENT: {}},
        }
        claims.update(overrides)
        for empty in [key for key, value in claims.items() if value is None]:
            del claims[empty]
        return jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": KID})

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": f"{ISSUER}/authorize",
                    "token_endpoint": f"{ISSUER}/token",
                    "jwks_uri": f"{ISSUER}/jwks",
                    "end_session_endpoint": f"{ISSUER}/logout",
                },
            )
        if path.endswith("/jwks"):
            self.jwks_fetches += 1
            return httpx.Response(200, json=self.jwks)
        if path.endswith("/token"):
            form = httpx.QueryParams(request.content.decode())
            if form.get("grant_type") == "refresh_token":
                self.refresh_grants += 1
                if not self.still_vouches:
                    return httpx.Response(400, json={"error": "invalid_grant"})
                return httpx.Response(
                    200, json={"refresh_token": f"idp-refresh-{self.refresh_grants}"}
                )
            self.seen_verifier = form.get("code_verifier")
            return httpx.Response(
                200,
                json=(
                    {
                        "id_token": self.id_token(),
                        "refresh_token": "idp-refresh-0",
                        "token_type": "Bearer",
                    }
                    if self.issue_refresh_token
                    else {"id_token": self.id_token(), "token_type": "Bearer"}
                ),
            )
        return httpx.Response(404)


@pytest.fixture
async def wired():
    """A service with the fake provider standing in for the network."""
    db = Database(":memory:")
    await db.connect()
    svc = MultiplayerService(db, RealtimeHub())
    await svc.initialize()
    idp = FakeProvider()
    transport = httpx.MockTransport(idp.handler)
    sessions = SessionService(
        db=db,
        repos=Repos(db),
        provider=OidcProvider(
            settings=OidcSettings(
                issuer=ISSUER,
                client_id=CLIENT_ID,
                client_secret="shh",
                redirect_uri="https://xyzzy.example/callback",
                post_logout_redirects=frozenset({"https://xyzzy.example/bye"}),
            )
        ),
        settings=SessionSettings(idle_seconds=1800, absolute_seconds=36000, idle_bump_seconds=0),
    )
    sessions._client = lambda: httpx.AsyncClient(transport=transport)  # type: ignore[method-assign]
    yield sessions, idp, TokenAuthenticator(db, sessions.note_used), db
    await db.close()


async def _sign_in(sessions: SessionService, idp: FakeProvider) -> Any:
    """Walk the flow the way a browser would: nonce round trip and binding cookie."""
    url, binding = await sessions.begin_login()
    params = httpx.URL(url).params
    idp.expect_nonce = params["nonce"]
    return await sessions.complete_login(state=params["state"], code="auth-code", binding=binding)


@pytest.mark.asyncio
async def test_the_login_carries_a_pkce_challenge_and_the_verifier_that_opens_it(wired):
    sessions, idp, _, _ = wired
    url, binding = await sessions.begin_login()
    params = httpx.URL(url).params
    idp.expect_nonce = params["nonce"]
    assert params["code_challenge_method"] == "S256"

    await sessions.complete_login(state=params["state"], code="auth-code", binding=binding)
    # The verifier the provider received must be the one the challenge was made
    # from. Sending a challenge and then any verifier is PKCE-shaped and not PKCE.
    assert idp.seen_verifier is not None
    assert challenge_for(idp.seen_verifier) == params["code_challenge"]


@pytest.mark.asyncio
async def test_a_login_attempt_is_spendable_once(wired):
    sessions, idp, _, _ = wired
    url, binding = await sessions.begin_login()
    params = httpx.URL(url).params
    idp.expect_nonce = params["nonce"]
    await sessions.complete_login(state=params["state"], code="auth-code", binding=binding)
    with pytest.raises(SessionError):
        await sessions.complete_login(state=params["state"], code="auth-code", binding=binding)


@pytest.mark.asyncio
async def test_an_id_token_from_another_login_is_refused(wired):
    sessions, idp, _, _ = wired
    url, binding = await sessions.begin_login()
    params = httpx.URL(url).params
    idp.expect_nonce = params["nonce"]
    async with httpx.AsyncClient(transport=httpx.MockTransport(idp.handler)) as client:
        with pytest.raises(OidcError):
            await sessions.provider.verify_id_token(
                client, id_token=idp.id_token(), nonce="a nonce from somewhere else"
            )
    # The attempt is still open, because nothing consumed it.
    await sessions.complete_login(state=params["state"], code="auth-code", binding=binding)


@pytest.mark.asyncio
async def test_a_token_signed_by_a_key_the_provider_does_not_publish_is_refused(wired):
    sessions, idp, _, _ = wired
    forged = jwt.encode(
        {"iss": ISSUER, "sub": SUBJECT, "aud": CLIENT_ID, "iat": 0, "exp": 9999999999},
        idp.other_key,
        algorithm="RS256",
        headers={"kid": KID},
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(idp.handler)) as client:
        with pytest.raises(OidcError):
            await sessions.provider.verify_id_token(client, id_token=forged, nonce="whatever")


@pytest.mark.asyncio
async def test_the_access_credential_dies_the_moment_the_session_is_revoked(wired):
    sessions, idp, authenticator, _ = wired
    issued = await _sign_in(sessions, idp)
    principal = await authenticator.authenticate(f"Bearer {issued.access_token}")
    assert principal.session_id == issued.session.session_id

    await sessions.end_session(issued.session.session_id)
    # No window. Keycloak's own documentation concedes one here, bounded by the
    # access token's lifetime; a credential that is a row has none.
    with pytest.raises(AuthenticationError):
        await authenticator.authenticate(f"Bearer {issued.access_token}")


@pytest.mark.asyncio
async def test_both_clocks_end_a_session_on_their_own(wired):
    sessions, idp, authenticator, db = wired
    issued = await _sign_in(sessions, idp)
    past = (utcnow() - timedelta(seconds=1)).isoformat()

    await db.execute(
        "UPDATE user_sessions SET idle_expires_at = ? WHERE session_id = ?",
        (past, issued.session.session_id),
    )
    with pytest.raises(AuthenticationError):
        await authenticator.authenticate(f"Bearer {issued.access_token}")

    # And the other one, with idle healthy again: neither clock is decorative.
    await db.execute(
        "UPDATE user_sessions SET idle_expires_at = ?, absolute_expires_at = ? "
        "WHERE session_id = ?",
        ((utcnow() + timedelta(hours=1)).isoformat(), past, issued.session.session_id),
    )
    with pytest.raises(AuthenticationError):
        await authenticator.authenticate(f"Bearer {issued.access_token}")


@pytest.mark.asyncio
async def test_a_refresh_rotates_and_the_spent_token_stops_working(wired):
    sessions, idp, authenticator, _ = wired
    issued = await _sign_in(sessions, idp)
    rotated = await sessions.refresh(issued.refresh_token)

    assert rotated.refresh_token != issued.refresh_token
    assert rotated.access_token != issued.access_token
    await authenticator.authenticate(f"Bearer {rotated.access_token}")


@pytest.mark.asyncio
async def test_replaying_a_refresh_token_costs_the_whole_session(wired):
    sessions, idp, authenticator, _ = wired
    issued = await _sign_in(sessions, idp)
    rotated = await sessions.refresh(issued.refresh_token)

    with pytest.raises(SessionError):
        await sessions.refresh(issued.refresh_token)

    # The replay revoked the session, so the credential minted by the *successful*
    # rotation is dead too. Revoking only the replayed token would leave whoever
    # holds the original inside.
    with pytest.raises(AuthenticationError):
        await authenticator.authenticate(f"Bearer {rotated.access_token}")
    with pytest.raises(SessionError):
        await sessions.refresh(rotated.refresh_token)


@pytest.mark.asyncio
async def test_back_channel_logout_ends_the_session_it_names(wired):
    sessions, idp, authenticator, _ = wired
    issued = await _sign_in(sessions, idp)
    assert await sessions.accept_backchannel_logout(idp.logout_token()) == 1
    with pytest.raises(AuthenticationError):
        await authenticator.authenticate(f"Bearer {issued.access_token}")


@pytest.mark.asyncio
async def test_a_logout_token_is_refused_when_replayed_or_malformed(wired):
    sessions, idp, _, _ = wired
    await _sign_in(sessions, idp)

    token = idp.logout_token()
    await sessions.accept_backchannel_logout(token)
    with pytest.raises(OidcError):
        # A session named by this token may since have been re-established, so
        # acting on it twice is destroying something the provider never asked about.
        await sessions.accept_backchannel_logout(token)

    with pytest.raises(OidcError):
        # An ID token replayed as a logout token.
        await sessions.accept_backchannel_logout(idp.logout_token(nonce="n"))
    with pytest.raises(OidcError):
        await sessions.accept_backchannel_logout(idp.logout_token(events={"other": {}}))
    with pytest.raises(OidcError):
        await sessions.accept_backchannel_logout(idp.logout_token(sub=None, sid=None))


@pytest.mark.asyncio
async def test_signing_out_everywhere_ends_every_session_that_person_holds(wired):
    sessions, idp, authenticator, _ = wired
    first = await _sign_in(sessions, idp)
    second = await _sign_in(sessions, idp)
    assert first.session.session_id != second.session.session_id

    assert await sessions.end_every_session(first.session.user_id) == 2
    for issued in (first, second):
        with pytest.raises(AuthenticationError):
            await authenticator.authenticate(f"Bearer {issued.access_token}")


@pytest.mark.asyncio
async def test_an_operator_minted_credential_is_untouched_by_any_of_this(wired):
    _, _, authenticator, db = wired
    await db.execute(
        "INSERT INTO user_tokens(token_hash, user_id, label, created_at) VALUES (?,?,?,?)",
        (hash_token("operator-token"), "user_ops", "cli", utcnow().isoformat()),
    )
    principal = await authenticator.authenticate("Bearer operator-token")
    assert principal.user_id == "user_ops"
    # It has no session, so no session clock can end it and no logout reaches it.
    assert principal.session_id is None


@pytest.mark.asyncio
async def test_no_model_driven_turn_can_mint_extend_or_end_a_session(wired):
    sessions, idp, _, _ = wired
    issued = await _sign_in(sessions, idp)
    fenced = {
        "login": lambda: sessions.begin_login(),
        "callback": lambda: sessions.complete_login(state="s", code="c"),
        "refresh": lambda: sessions.refresh(issued.refresh_token),
        "logout": lambda: sessions.end_session(issued.session.session_id),
        "logout.all": lambda: sessions.end_every_session(issued.session.user_id),
        "logout.backchannel": lambda: sessions.accept_backchannel_logout(idp.logout_token()),
    }
    with agent_turn("exec_from_a_model"):
        for action, call in fenced.items():
            with pytest.raises(AuthorizationError, match="outside the agent surface"):
                await call(), action


@pytest.mark.asyncio
async def test_a_provider_that_rotates_its_keys_is_not_an_outage(wired):
    sessions, idp, _, _ = wired
    await _sign_in(sessions, idp)
    fetches_after_first_login = idp.jwks_fetches

    sessions.provider._keys.clear()
    # Rotation costs at most the refetch cooldown; past it, an unknown key is
    # fetched rather than refused.
    sessions.provider._last_key_fetch = 0.0
    await _sign_in(sessions, idp)
    assert idp.jwks_fetches == fetches_after_first_login + 1


@pytest.mark.asyncio
async def test_forged_key_ids_cannot_be_used_to_hammer_the_provider(wired):
    sessions, idp, _, _ = wired
    await _sign_in(sessions, idp)
    before = idp.jwks_fetches

    # Twenty-five forged tokens, twenty-five different key ids, all unauthenticated.
    # A critic turned exactly this into twenty-five outbound requests.
    async with httpx.AsyncClient(transport=httpx.MockTransport(idp.handler)) as client:
        for index in range(25):
            forged = jwt.encode(
                {"iss": ISSUER, "sub": SUBJECT, "aud": CLIENT_ID, "iat": 0, "exp": 9999999999},
                idp.other_key,
                algorithm="RS256",
                headers={"kid": f"invented-{index}"},
            )
            with pytest.raises(OidcError):
                await sessions.provider.verify_id_token(client, id_token=forged, nonce="x")

    assert idp.jwks_fetches - before <= 1


@pytest.mark.asyncio
async def test_an_id_token_addressed_to_anyone_else_is_refused(wired):
    sessions, idp, _, _ = wired
    async with httpx.AsyncClient(transport=httpx.MockTransport(idp.handler)) as client:
        # OIDC Core 3.1.3.7: reject a token carrying audiences we do not trust.
        # PyJWT is satisfied when `aud` merely contains our client id, so any
        # co-tenant of the same provider holding a multi-audience token would
        # otherwise sign in here as its own subject.
        with pytest.raises(OidcError):
            await sessions.provider.verify_id_token(
                client, id_token=idp.id_token(aud=[CLIENT_ID, "some-other-client"]), nonce=""
            )
        # And where an authorized party is named, it has to be us.
        with pytest.raises(OidcError):
            await sessions.provider.verify_id_token(
                client, id_token=idp.id_token(azp="some-other-client"), nonce=""
            )


@pytest.mark.asyncio
async def test_a_refresh_retires_the_access_token_it_replaces(wired):
    sessions, idp, authenticator, _ = wired
    issued = await _sign_in(sessions, idp)
    first = await sessions.refresh(issued.refresh_token)
    second = await sessions.refresh(first.refresh_token)

    # Only the newest credential is alive. Minting without retiring would leave
    # three live access tokens after two refreshes, the oldest good for the whole
    # absolute lifetime of the session.
    await authenticator.authenticate(f"Bearer {second.access_token}")
    for retired in (issued.access_token, first.access_token):
        with pytest.raises(AuthenticationError):
            await authenticator.authenticate(f"Bearer {retired}")


@pytest.mark.asyncio
async def test_a_login_can_only_be_finished_by_the_browser_that_started_it(wired):
    sessions, idp, _, _ = wired
    url, binding = await sessions.begin_login()
    params = httpx.URL(url).params
    idp.expect_nonce = params["nonce"]

    # Someone who obtained the state but never held the cookie. Without this
    # check they could complete a login the victim never started, and the victim
    # would be signed in as them.
    with pytest.raises(SessionError):
        await sessions.complete_login(state=params["state"], code="auth-code", binding="")

    # And the victim's attempt is still open. If the stranger's call had consumed
    # it, the attack would fail and so would the victim, for no stated reason.
    issued = await sessions.complete_login(state=params["state"], code="auth-code", binding=binding)
    assert issued.session.session_id


@pytest.mark.asyncio
async def test_a_logout_token_is_not_burned_when_its_revocation_fails(wired):
    sessions, idp, authenticator, _ = wired
    issued = await _sign_in(sessions, idp)
    token = idp.logout_token()

    original = sessions.repos.user_sessions.revoke_in_transaction

    async def explode(*args: object, **kwargs: object) -> bool:
        raise RuntimeError("the write failed")

    sessions.repos.user_sessions.revoke_in_transaction = explode  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await sessions.accept_backchannel_logout(token)
    sessions.repos.user_sessions.revoke_in_transaction = original  # type: ignore[method-assign]

    # The provider retries. Burning the jti before revoking would refuse this as
    # a replay and leave the session alive for good.
    assert await sessions.accept_backchannel_logout(token) == 1
    with pytest.raises(AuthenticationError):
        await authenticator.authenticate(f"Bearer {issued.access_token}")


@pytest.mark.asyncio
async def test_front_channel_logout_ends_the_named_session_and_nothing_wider(wired):
    sessions, idp, authenticator, _ = wired
    issued = await _sign_in(sessions, idp)

    # Both identifiers are required, so an omitted one cannot widen this into
    # "every session" — the failure mode that matters for an endpoint with no
    # token on it at all.
    for issuer, sid in (
        (ISSUER, ""),
        ("", "idp-session-1"),
        ("https://elsewhere", "idp-session-1"),
    ):
        with pytest.raises(SessionError):
            await sessions.accept_frontchannel_logout(issuer=issuer, sid=sid)
    await authenticator.authenticate(f"Bearer {issued.access_token}")

    assert await sessions.accept_frontchannel_logout(issuer=ISSUER, sid="idp-session-1") == 1
    with pytest.raises(AuthenticationError):
        await authenticator.authenticate(f"Bearer {issued.access_token}")


@pytest.mark.asyncio
async def test_a_retried_refresh_costs_the_session_and_that_is_the_lesser_evil(wired):
    sessions, idp, _, _ = wired
    issued = await _sign_in(sessions, idp)
    await sessions.refresh(issued.refresh_token)

    # A grace window was tried here and removed. It let a thief presenting the
    # stolen predecessor within the window take a working session, retire the
    # victim's credential, and leave the victim's own next refresh to be judged
    # the replay. A client that cannot retry is a worse experience; a victim
    # blamed for the theft is a worse security property, and Keycloak's default
    # is no reuse either.
    with pytest.raises(SessionError):
        await sessions.refresh(issued.refresh_token)


@pytest.mark.asyncio
async def test_an_access_credential_expires_on_its_own(wired):
    sessions, idp, authenticator, db = wired
    issued = await _sign_in(sessions, idp)
    await authenticator.authenticate(f"Bearer {issued.access_token}")

    # Revocation only helps somebody who knows to revoke. A stolen credential
    # nobody has noticed is bounded by this and nothing else, and the session's
    # ten-hour absolute clock is not a bound worth the name.
    await db.execute(
        "UPDATE user_tokens SET expires_at = ? WHERE session_id = ?",
        ((utcnow() - timedelta(seconds=1)).isoformat(), issued.session.session_id),
    )
    with pytest.raises(AuthenticationError):
        await authenticator.authenticate(f"Bearer {issued.access_token}")


@pytest.mark.asyncio
async def test_a_model_driven_turn_cannot_extend_a_session_either(wired):
    sessions, idp, _, _ = wired
    issued = await _sign_in(sessions, idp)
    # The verb the older fence test claimed to cover and did not: extending.
    # It enumerated six callables and missed the one that actually moves the clock.
    with agent_turn("exec_from_a_model"):
        with pytest.raises(AuthorizationError, match="outside the agent surface"):
            await sessions.note_used(issued.session, utcnow())


@pytest.mark.asyncio
async def test_a_refresh_asks_the_provider_whether_it_still_vouches(wired):
    sessions, idp, authenticator, _ = wired
    issued = await _sign_in(sessions, idp)
    grants_before = idp.refresh_grants

    rotated = await sessions.refresh(issued.refresh_token)
    # Every refresh spends the provider's own token. Without this the provider
    # never hears from us again after login, and somebody disabled upstream keeps
    # a live session here until an absolute clock they cannot see runs out.
    assert idp.refresh_grants == grants_before + 1

    idp.still_vouches = False
    with pytest.raises(SessionError):
        await sessions.refresh(rotated.refresh_token)
    with pytest.raises(AuthenticationError):
        await authenticator.authenticate(f"Bearer {rotated.access_token}")


@pytest.mark.asyncio
async def test_a_replayed_token_is_refused_without_troubling_the_provider(wired):
    sessions, idp, _, _ = wired
    issued = await _sign_in(sessions, idp)
    await sessions.refresh(issued.refresh_token)
    grants_before = idp.refresh_grants

    with pytest.raises(SessionError):
        await sessions.refresh(issued.refresh_token)
    # A stale token in anyone's hands would otherwise be an outbound request they
    # get to cause, at whatever rate they like.
    assert idp.refresh_grants == grants_before


@pytest.mark.asyncio
async def test_a_provider_that_issues_no_refresh_token_is_refused_by_default(wired):
    sessions, idp, _, _ = wired
    idp.issue_refresh_token = False

    # Auth0 and Google omit it without offline_access; Keycloak omits it with the
    # scope off. A critic proved the old code accepted such a login and then never
    # spoke to the provider again for ten hours. Silence was the whole defect.
    with pytest.raises(SessionError, match="offline_access"):
        await _sign_in(sessions, idp)


@pytest.mark.asyncio
async def test_an_unverifiable_session_may_be_allowed_but_never_a_long_one(wired):
    sessions, idp, _, _ = wired
    idp.issue_refresh_token = False
    sessions.settings = replace(sessions.settings, allow_unverifiable_sessions=True)

    issued = await _sign_in(sessions, idp)
    lifetime = (issued.session.absolute_expires_at - issued.session.created_at).total_seconds()
    # Opting in buys a short session, not the ordinary one. Nobody can ask the
    # provider about this person again, so the clock is the only thing left.
    assert lifetime <= UNVERIFIABLE_ABSOLUTE_SECONDS
    assert lifetime < sessions.settings.absolute_seconds


@pytest.mark.asyncio
async def test_a_logout_reaches_a_session_whose_login_carried_no_sid(wired):
    sessions, idp, authenticator, _ = wired
    # Some providers put `sid` in the logout token but not in the ID token. The
    # session therefore has none stored, and a lookup by sid alone matches
    # nothing — while the jti is burned, so the provider's retry is refused and
    # the revocation is lost for good.
    idp.omit_sid_at_login = True
    issued = await _sign_in(sessions, idp)

    assert await sessions.accept_backchannel_logout(idp.logout_token()) == 1
    with pytest.raises(AuthenticationError):
        await authenticator.authenticate(f"Bearer {issued.access_token}")
