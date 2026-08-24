"""The session lifecycle: what a sign-in creates, refreshes, and destroys.

:mod:`multiplayer.security.oidc` answers whether a token is genuine. This module
decides what that means for a person's access, and it is the only place that
mints or kills a session.

Every method here opens with ``require_human_boundary``. Creating a session,
extending one, or ending somebody else's are governance actions, and a
model-driven turn must not reach them through any path — including one added
later by someone who did not read this file.

The credential a session hands out is a ``user_tokens`` row, the same shape an
operator-minted credential already had. That is deliberate: a self-validating
token would be readable without the database, and a credential that can be
accepted without reading the database is a credential a revocation cannot reach.
Keycloak's documentation concedes exactly that window — a revoked session keeps
working until its access token expires, up to five minutes by default.

Here a revoked session fails the next HTTP request, with no window at all. One
honest exception: a WebSocket that is *already open* re-authenticates on a
thirty-second heartbeat, so a live socket can outlive its revocation by up to
that long. An earlier version of this file claimed zero. A critic measured three
room events delivered after revocation and was right to call it false.
"""

from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import httpx

from ..db.connection import Database
from ..db.repositories import Repos
from ..domain.models import (
    OidcAuthorization,
    SessionRefreshToken,
    User,
    UserSession,
    new_id,
    utcnow,
)
from .auth import hash_token
from .boundary import require_human_boundary
from .oidc import OidcError, OidcProvider, generate_verifier

log = logging.getLogger(__name__)

# Both defaults are Keycloak's, so a deployment that changes neither behaves the
# way an operator who knows Keycloak expects.
DEFAULT_IDLE_SECONDS = 1800
DEFAULT_ABSOLUTE_SECONDS = 36000
DEFAULT_LOGIN_WINDOW_SECONDS = 600
# Keycloak's access token lifespan. Short on purpose: revocation only helps
# somebody who knows to revoke, and a stolen credential nobody has noticed is
# bounded by nothing else. A short life forces the thief through the refresh
# rotation, where reuse detection is waiting.
DEFAULT_ACCESS_SECONDS = 300
# What a session is worth when the provider issued no refresh token, so it can
# never be re-checked. Fifteen minutes is short enough that "the provider has
# stopped vouching for this person" cannot go unnoticed for long, and the
# alternative — a ten-hour session nobody can revalidate — is the failure a
# critic found by having a provider simply omit the token.
UNVERIFIABLE_ABSOLUTE_SECONDS = 900
# There is deliberately no reuse grace window. One was tried and removed: a
# thief presenting the stolen predecessor within the window received a working
# session, silently retired the victim's credential, and left the victim's own
# next refresh to be judged the replay. Trading a client's self-inflicted
# failure for a window in which theft succeeds and is blamed on the victim is
# the worse of the two, and Keycloak's own default is no reuse either.
#
# The cost is real and belongs to the client: a refresh whose answer is lost
# cannot be retried, and the person signs in again.


class SessionError(ValueError):
    """A session that cannot be established or continued."""


@dataclass(frozen=True, slots=True)
class SessionSettings:
    idle_seconds: int = DEFAULT_IDLE_SECONDS
    absolute_seconds: int = DEFAULT_ABSOLUTE_SECONDS
    access_seconds: int = DEFAULT_ACCESS_SECONDS
    login_window_seconds: int = DEFAULT_LOGIN_WINDOW_SECONDS
    # Whether to accept a login the provider gave us no way to re-check. Off, so
    # that the degradation is a refusal an operator reads rather than a property
    # nobody notices.
    allow_unverifiable_sessions: bool = False
    # A session used constantly would otherwise write its idle clock on every
    # request. The clock is still honoured to the second; only the write is coarse.
    idle_bump_seconds: int = 60


def settings_from_environment() -> SessionSettings:
    """Session clocks a deployment can set, defaulting to Keycloak's numbers."""

    def seconds(name: str, default: int) -> int:
        raw = os.environ.get(name, "").strip()
        if not raw:
            return default
        try:
            value = int(raw)
        except ValueError as exc:
            raise RuntimeError(f"{name} must be a whole number of seconds") from exc
        if value < 1:
            raise RuntimeError(f"{name} must be at least 1")
        return value

    return SessionSettings(
        idle_seconds=seconds("XYZZY_SESSION_IDLE_SECONDS", DEFAULT_IDLE_SECONDS),
        absolute_seconds=seconds("XYZZY_SESSION_ABSOLUTE_SECONDS", DEFAULT_ABSOLUTE_SECONDS),
        access_seconds=seconds("XYZZY_SESSION_ACCESS_SECONDS", DEFAULT_ACCESS_SECONDS),
        allow_unverifiable_sessions=os.environ.get(
            "XYZZY_OIDC_ALLOW_UNVERIFIABLE_SESSIONS", ""
        ).strip()
        in {"1", "true", "yes"},
    )


@dataclass(frozen=True, slots=True)
class IssuedSession:
    """The two credentials a caller receives, in plaintext, exactly once."""

    session: UserSession
    access_token: str
    refresh_token: str


def _new_credential() -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    return token, hash_token(token)


@dataclass
class SessionService:
    db: Database
    repos: Repos
    provider: OidcProvider
    settings: SessionSettings = field(default_factory=SessionSettings)

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=10.0)

    # ── Establishing a session ───────────────────────────────────────────────

    async def begin_login(self) -> tuple[str, str]:
        """Start a login: where to send the browser, and the cookie to give it.

        The state, nonce and PKCE verifier are written here and read back from
        the row at the callback. Nothing that comes back in the request is
        trusted to describe the attempt it claims to belong to.

        The second return value binds the attempt to *this* browser. State alone
        lives on the server and proves only that some attempt exists; anyone who
        obtains a state value can otherwise complete a login the victim never
        started, leaving them signed in as the attacker. The caller sets it as an
        HttpOnly cookie and hands it back at the callback.
        """
        require_human_boundary("sso.login")
        self.provider.require_configured()
        moment = utcnow()
        binding = secrets.token_urlsafe(24)
        authorization = OidcAuthorization(
            state=secrets.token_urlsafe(24),
            nonce=secrets.token_urlsafe(24),
            code_verifier=generate_verifier(),
            browser_binding_hash=hash_token(binding),
            created_at=moment,
            expires_at=moment + timedelta(seconds=self.settings.login_window_seconds),
        )
        await self.repos.user_sessions.start_authorization(authorization)
        async with self._client() as client:
            url = await self.provider.authorization_url(
                client,
                state=authorization.state,
                nonce=authorization.nonce,
                verifier=authorization.code_verifier,
            )
        return url, binding

    async def complete_login(self, *, state: str, code: str, binding: str = "") -> IssuedSession:
        """Finish a login: consume the attempt, verify the token, mint a session."""
        require_human_boundary("sso.callback")
        self.provider.require_configured()
        moment = utcnow()
        # The binding is part of the claim: a stranger holding a state value must
        # not be able to consume a login somebody else is in the middle of.
        attempt = await self.repos.user_sessions.claim_authorization(
            state, moment, hash_token(binding) if binding else ""
        )
        if attempt is None:
            # Unknown, already used, or begun by a different browser. All three
            # are answered the same way, because telling them apart tells an
            # attacker which states exist and which are still open.
            raise SessionError("this login attempt is not open")
        if moment > attempt.expires_at:
            raise SessionError("this login attempt expired")

        async with self._client() as client:
            tokens = await self.provider.exchange_code(
                client, code=code, verifier=attempt.code_verifier
            )
            claims = await self.provider.verify_id_token(
                client, id_token=str(tokens["id_token"]), nonce=attempt.nonce
            )
        return await self._establish(
            claims, moment, str(tokens["id_token"]), str(tokens.get("refresh_token") or "")
        )

    async def _establish(
        self,
        claims: dict[str, Any],
        moment: datetime,
        id_token: str = "",
        provider_refresh: str = "",
    ) -> IssuedSession:
        user_id = self.user_id_for(self.provider.settings.issuer, str(claims["sub"]))
        # No refresh token means the provider has given us no way to ask about
        # this person again. Accepting that silently is how a session outlives an
        # upstream disable by the whole absolute clock, so it is either refused
        # or made short — never quietly ordinary.
        absolute_seconds = self.settings.absolute_seconds
        if not provider_refresh:
            if not self.settings.allow_unverifiable_sessions:
                raise SessionError(
                    "the provider issued no refresh token, so this session could never be "
                    "re-checked; request offline_access, or set "
                    "XYZZY_OIDC_ALLOW_UNVERIFIABLE_SESSIONS to accept short ones"
                )
            absolute_seconds = min(absolute_seconds, UNVERIFIABLE_ABSOLUTE_SECONDS)
            log.warning(
                "Session for %s cannot be revalidated: the provider issued no refresh token. "
                "Capping it at %s seconds.",
                user_id,
                absolute_seconds,
            )
        access_token, access_hash = _new_credential()
        refresh_token, refresh_hash = _new_credential()
        session = UserSession(
            session_id=new_id("usess"),
            user_id=user_id,
            issuer=self.provider.settings.issuer,
            subject=str(claims["sub"]),
            idp_session_id=str(claims["sid"]) if claims.get("sid") else None,
            created_at=moment,
            idle_expires_at=moment + timedelta(seconds=self.settings.idle_seconds),
            absolute_expires_at=moment + timedelta(seconds=absolute_seconds),
            idp_id_token=id_token,
            idp_refresh_token=provider_refresh,
        )
        async with self.db.transaction():
            if await self.repos.users.get(user_id) is None:
                await self.repos.users.create(
                    User(
                        user_id=user_id,
                        display_name=str(
                            claims.get("name") or claims.get("preferred_username") or user_id
                        ),
                        email=str(claims.get("email") or ""),
                        created_at=moment,
                    )
                )
            await self.repos.user_sessions.create_in_transaction(
                session,
                access_hash,
                SessionRefreshToken(
                    token_hash=refresh_hash,
                    session_id=session.session_id,
                    issued_at=moment,
                    expires_at=session.absolute_expires_at,
                ),
                min(
                    moment + timedelta(seconds=self.settings.access_seconds),
                    session.absolute_expires_at,
                ),
            )
        return IssuedSession(
            session=session, access_token=access_token, refresh_token=refresh_token
        )

    @staticmethod
    def user_id_for(issuer: str, subject: str) -> str:
        """A stable local identity for one provider's subject.

        Keyed on the issuer and the subject, never on the email address. An
        email claim can be unverified, can be reassigned, and is the standard way
        an account gets taken over by someone who registered the same address
        somewhere else. Linking an SSO login to an operator-minted account is a
        deliberate act, not something inferred from a string that happens to match.
        """
        digest = hash_token(f"{issuer}|{subject}")
        return f"usr_{digest[:32]}"

    # ── Continuing one ───────────────────────────────────────────────────────

    async def refresh(self, presented: str) -> IssuedSession:
        """Rotate a refresh token. A replay costs the whole session.

        Keycloak invalidates the session on reuse rather than the token, and it is
        right to: a token presented twice means a copy exists somewhere it should
        not, and revoking only the copy leaves whoever holds the original inside.

        The revocation happens in its own transaction, after this one, because a
        revocation followed by a raise inside the same transaction is a rollback:
        the session comes back to life and the replay is answered with an error
        message and nothing else. A test caught exactly that.
        """
        require_human_boundary("sso.refresh")
        moment = utcnow()
        presented_hash = hash_token(presented)
        doomed: tuple[str, str] | None = None

        # Read before spending anything, so a replayed token is refused without
        # reaching the provider: otherwise a stale token in anyone's hands is an
        # outbound request they get to cause.
        prior = await self.repos.user_sessions.get_refresh(presented_hash)
        if prior is None:
            raise SessionError("this refresh token is not recognised")
        prior_session = await self.repos.user_sessions.get(prior.session_id)
        if prior_session is None:
            raise SessionError("this refresh token is not recognised")
        provider_tokens = await self._revalidate(prior_session, prior, moment)

        async with self.db.transaction():
            stored = await self.repos.user_sessions.get_refresh(presented_hash)
            if stored is None:
                raise SessionError("this refresh token is not recognised")
            session = await self.repos.user_sessions.get(stored.session_id)
            if session is None:
                raise SessionError("this refresh token is not recognised")

            access_token, access_hash = _new_credential()
            refresh_token, refresh_hash = _new_credential()
            claimed = await self.repos.user_sessions.claim_refresh_in_transaction(
                presented_hash, refresh_hash, moment
            )
            if not claimed:
                doomed = (session.session_id, "refresh token replayed")
            elif not session.alive_at(moment) or moment > stored.expires_at:
                doomed = (session.session_id, "expired")

            rotated = UserSession(
                session_id=session.session_id,
                user_id=session.user_id,
                issuer=session.issuer,
                subject=session.subject,
                idp_session_id=session.idp_session_id,
                created_at=session.created_at,
                idle_expires_at=min(
                    moment + timedelta(seconds=self.settings.idle_seconds),
                    session.absolute_expires_at,
                ),
                absolute_expires_at=session.absolute_expires_at,
            )
            if doomed is None:
                await self.db.execute(
                    "INSERT INTO user_tokens(token_hash, user_id, label, created_at, session_id, "
                    "expires_at) VALUES (?, ?, 'sso', ?, ?, ?)",
                    (
                        access_hash,
                        session.user_id,
                        moment.isoformat(),
                        session.session_id,
                        min(
                            moment + timedelta(seconds=self.settings.access_seconds),
                            session.absolute_expires_at,
                        ).isoformat(),
                    ),
                )
                await self.repos.user_sessions.issue_refresh_in_transaction(
                    SessionRefreshToken(
                        token_hash=refresh_hash,
                        session_id=session.session_id,
                        issued_at=moment,
                        expires_at=session.absolute_expires_at,
                    )
                )
                await self.db.execute(
                    "UPDATE user_sessions SET idle_expires_at = ? WHERE session_id = ?",
                    (rotated.idle_expires_at.isoformat(), session.session_id),
                )
                # Minting without retiring is accumulation, not rotation.
                await self.repos.user_sessions.supersede_access_tokens_in_transaction(
                    session.session_id, moment, access_hash
                )
                # Providers rotate their own refresh tokens; keep whichever one
                # the next revalidation has to present.
                if provider_tokens.get("refresh_token"):
                    await self.db.execute(
                        "UPDATE user_sessions SET idp_refresh_token = ? WHERE session_id = ?",
                        (str(provider_tokens["refresh_token"]), session.session_id),
                    )

        if doomed is not None:
            session_id, reason = doomed
            async with self.db.transaction():
                await self.repos.user_sessions.revoke_in_transaction(session_id, reason, moment)
            log.warning("Session %s revoked: %s", session_id, reason)
            raise SessionError(
                "this refresh token was already spent"
                if reason == "refresh token replayed"
                else "this session has ended"
            )
        return IssuedSession(
            session=rotated, access_token=access_token, refresh_token=refresh_token
        )

    async def _revalidate(
        self, session: UserSession, prior: SessionRefreshToken, moment: datetime
    ) -> dict[str, Any]:
        """Ask the provider whether it still vouches for this person.

        This is the only moment after login that the provider gets a say. Without
        it a session is decided once and never revisited, so somebody disabled,
        locked out, or password-reset upstream keeps a working session here until
        an absolute clock they cannot see expires. Keycloak's refresh grant
        re-checks the user session on every refresh; this is the same check.

        A replayed token never reaches here — the caller reads it first — so a
        stale token in a stranger's hands cannot be turned into outbound traffic.
        """
        if prior.consumed_at is not None or not session.idp_refresh_token:
            return {}
        try:
            async with self._client() as client:
                return await self.provider.refresh_at_provider(
                    client, refresh_token=session.idp_refresh_token
                )
        except OidcError:
            async with self.db.transaction():
                await self.repos.user_sessions.revoke_in_transaction(
                    session.session_id, "the provider withdrew this session", moment
                )
            log.info("Session %s ended: the provider refused to refresh it", session.session_id)
            raise SessionError("this session has ended") from None

    async def note_used(self, session: UserSession, moment: datetime) -> None:
        """Push the idle clock, at most once per bump interval.

        Fenced, despite sitting on the authenticate path. The first version of
        this method was left outside the boundary on the reasoning that
        authenticating a request is not governance — but this method does not
        authenticate anything, it *extends a session*, which is the middle verb in
        the sentence the fence test claims to enforce. A critic set an agent turn
        and watched a model-driven path push a human's idle clock forward, while
        the test that was supposed to catch it enumerated six other callables.

        A structural guard that asserts over a closed enumeration cannot see the
        call site nobody added to the enumeration. This one is now inside the
        fence itself, where the ambient context decides rather than a list.
        """
        require_human_boundary("sso.session.extend")
        target = moment + timedelta(seconds=self.settings.idle_seconds)
        if (target - session.idle_expires_at).total_seconds() < self.settings.idle_bump_seconds:
            return
        await self.repos.user_sessions.touch_idle(
            session.session_id, min(target, session.absolute_expires_at)
        )

    # ── Ending one ───────────────────────────────────────────────────────────

    async def end_session(self, session_id: str, reason: str = "signed out") -> bool:
        require_human_boundary("sso.logout")
        moment = utcnow()
        async with self.db.transaction():
            return await self.repos.user_sessions.revoke_in_transaction(session_id, reason, moment)

    async def end_every_session(self, user_id: str, reason: str = "signed out everywhere") -> int:
        require_human_boundary("sso.logout.all")
        moment = utcnow()
        async with self.db.transaction():
            session_ids = await self.repos.user_sessions.live_session_ids(user_id=user_id)
            for session_id in session_ids:
                await self.repos.user_sessions.revoke_in_transaction(session_id, reason, moment)
        return len(session_ids)

    async def accept_backchannel_logout(self, logout_token: str) -> int:
        """Act on the provider's word that a session is over.

        Answers with how many sessions were revoked. A replayed token is refused
        rather than reapplied, because the session it named may since have been
        legitimately re-established, and killing that one is the damage.
        """
        require_human_boundary("sso.logout.backchannel")
        self.provider.require_configured()
        moment = utcnow()
        async with self._client() as client:
            claims = await self.provider.verify_logout_token(client, logout_token=logout_token)

        issuer = str(claims["iss"]).rstrip("/")
        async with self.db.transaction():
            # Remembering the token and acting on it are one write. Burning the
            # jti first and revoking afterwards loses the revocation for good if
            # anything between them fails: the retry is refused as a replay while
            # the session it named keeps working.
            if not await self.repos.user_sessions.remember_logout_token_in_transaction(
                str(claims["jti"]), issuer, moment
            ):
                raise OidcError("this logout token was already acted on")
            # A logout token may carry sid, sub, or both. Branching on the token
            # alone was wrong: whether we can match on sid depends on whether the
            # *ID token at login* carried one, and providers that omit it there
            # still send it here. The sid lookup then matched nothing, the jti was
            # burned, and the provider's retry was refused as a replay — a
            # revocation lost for good. So sid is tried first and sub is the
            # fallback, rather than the alternative.
            targets: list[str] = []
            if claims.get("sid"):
                targets = await self.repos.user_sessions.live_session_ids(
                    issuer=issuer, idp_session_id=str(claims["sid"])
                )
            if not targets and claims.get("sub"):
                targets = await self.repos.user_sessions.live_session_ids(
                    issuer=issuer, subject=str(claims["sub"])
                )
            for session_id in targets:
                await self.repos.user_sessions.revoke_in_transaction(
                    session_id, "back-channel logout", moment
                )
        return len(targets)

    async def accept_frontchannel_logout(self, *, issuer: str, sid: str) -> int:
        """The provider ending a session through the browser, per Front-Channel Logout 1.0.

        There is no token here — the specification's own design is an iframe with
        query parameters — so this endpoint is deliberately capable of one thing
        and nothing else: revoking sessions that a given issuer named by sid. Both
        are required, so an omitted argument can never widen it, and the worst a
        forged call achieves is signing somebody out, which is the safe direction
        for an unauthenticated request to fail in.
        """
        require_human_boundary("sso.logout.frontchannel")
        self.provider.require_configured()
        if not issuer or not sid:
            raise SessionError("front-channel logout needs both an issuer and a session")
        if issuer.rstrip("/") != self.provider.settings.issuer:
            raise SessionError("that issuer is not this deployment's provider")
        moment = utcnow()
        async with self.db.transaction():
            targets = await self.repos.user_sessions.live_session_ids(
                issuer=issuer.rstrip("/"), idp_session_id=sid
            )
            for session_id in targets:
                await self.repos.user_sessions.revoke_in_transaction(
                    session_id, "front-channel logout", moment
                )
        return len(targets)

    def permits_redirect(self, target: str) -> bool:
        """Only a target the deployment configured. Anything else is an open redirect."""
        return target in self.provider.settings.post_logout_redirects
