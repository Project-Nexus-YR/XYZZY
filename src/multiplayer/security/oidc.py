"""The relying-party half of a sign-in: discovery, PKCE, and token validation.

XYZZY is a relying party and never an identity provider. Every deployment
already has one, and building a second would be a second product.

Nothing here touches the database or mints a session. This module answers one
question at a time — what is the provider's configuration, is this ID token
genuinely about this login attempt, is this logout token genuinely from the
provider — and :mod:`multiplayer.security.sessions` decides what to do about the
answers. Keeping the cryptography away from the session lifecycle is what makes
each of them testable without the other.

Signature verification is PyJWT's. Hand-rolling JWS to avoid one dependency is
how implementations end up accepting ``alg: none``; the algorithms are named
explicitly here rather than taken from the token, because a token that chooses
its own verification algorithm has chosen not to be verified.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import jwt

# Asymmetric only. A symmetric algorithm verified against a key fetched from the
# issuer's JWKS is a confusion attack waiting for a provider that publishes one.
ALLOWED_ALGORITHMS = ("RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "PS256")

# The event that makes a logout token a logout token.
BACKCHANNEL_LOGOUT_EVENT = "http://schemas.openid.net/event/backchannel-logout"

# Enough for ordinary clock drift between two servers, not enough to be a window.
CLOCK_SKEW_SECONDS = 60

DISCOVERY_PATH = "/.well-known/openid-configuration"

# How often an unknown key id may cause an outbound fetch. Long enough that a
# stream of forged tokens cannot use us to hammer the provider, short enough
# that a real key rotation is a blip rather than an outage.
KEY_REFETCH_COOLDOWN_SECONDS = 30.0


class OidcError(ValueError):
    """A login that cannot be trusted. Never carries provider detail to the caller."""


class OidcNotConfigured(OidcError):
    """No identity provider is configured, so there is no SSO to attempt."""


@dataclass(frozen=True, slots=True)
class OidcSettings:
    """What a deployment says about its provider.

    ``post_logout_redirects`` is an allowlist rather than a single value because
    a redirect target accepted from the request is an open redirect, and one
    accepted from configuration is a decision somebody made on purpose.
    """

    issuer: str
    client_id: str
    client_secret: str
    redirect_uri: str
    scopes: tuple[str, ...] = ("openid", "profile", "email")
    post_logout_redirects: frozenset[str] = frozenset()

    @property
    def configured(self) -> bool:
        return bool(self.issuer and self.client_id and self.redirect_uri)


def settings_from_environment() -> OidcSettings:
    """Read the provider from the environment. Absent means SSO is simply off."""
    raw_scopes = os.environ.get("XYZZY_OIDC_SCOPES", "openid profile email").split()
    redirects = [
        target.strip()
        for target in os.environ.get("XYZZY_OIDC_POST_LOGOUT_REDIRECTS", "").split(",")
        if target.strip()
    ]
    return OidcSettings(
        issuer=os.environ.get("XYZZY_OIDC_ISSUER", "").rstrip("/"),
        client_id=os.environ.get("XYZZY_OIDC_CLIENT_ID", ""),
        client_secret=os.environ.get("XYZZY_OIDC_CLIENT_SECRET", ""),
        redirect_uri=os.environ.get("XYZZY_OIDC_REDIRECT_URI", ""),
        scopes=tuple(raw_scopes) or ("openid",),
        post_logout_redirects=frozenset(redirects),
    )


def generate_verifier() -> str:
    """A PKCE verifier: 43-128 characters of unreserved ASCII (RFC 7636 §4.1)."""
    return base64.urlsafe_b64encode(secrets.token_bytes(48)).decode("ascii").rstrip("=")


def challenge_for(verifier: str) -> str:
    """S256 only. ``plain`` is permitted by the RFC and is not a challenge."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


@dataclass
class OidcProvider:
    """One provider, with its discovery document and signing keys cached.

    The cache is per process and refreshed on a key the cache does not know,
    which is what makes a provider's key rotation invisible here rather than an
    outage. A key that is still unknown after one refresh is a token this
    provider did not sign.
    """

    settings: OidcSettings
    _document: dict[str, Any] | None = field(default=None, repr=False)
    _keys: dict[str, Any] = field(default_factory=dict, repr=False)
    _last_key_fetch: float = field(default=0.0, repr=False)

    def require_configured(self) -> None:
        if not self.settings.configured:
            raise OidcNotConfigured("no identity provider is configured")

    async def document(self, client: httpx.AsyncClient) -> dict[str, Any]:
        self.require_configured()
        if self._document is None:
            response = await client.get(self.settings.issuer + DISCOVERY_PATH)
            response.raise_for_status()
            document: dict[str, Any] = response.json()
            # The issuer in the document is the one that must appear in tokens.
            # A document served from one origin claiming to speak for another is
            # the whole reason this comparison exists.
            if str(document.get("issuer", "")).rstrip("/") != self.settings.issuer:
                raise OidcError("provider discovery document disowns its own issuer")
            self._document = document
        return self._document

    async def _fetch_keys(self, client: httpx.AsyncClient) -> None:
        """Read the provider's signing keys over the client this module was given.

        PyJWT's own JWKS client reaches for urllib, which would ignore the
        timeout configured here and open a second, unobservable way out of the
        process. One HTTP client, one timeout, one thing to point a test at.
        """
        document = await self.document(client)
        response = await client.get(str(document["jwks_uri"]))
        response.raise_for_status()
        self._keys = {
            str(key.key_id): key.key
            for key in jwt.PyJWKSet.from_dict(response.json()).keys
            if key.key_id
        }

    async def signing_key(self, client: httpx.AsyncClient, token: str) -> Any:
        """The key this token names, refetching once for a key we have not seen.

        One rotation's worth of forgiveness: a provider that rolled its keys
        should not be an outage. A key still unknown after a refetch means the
        token was signed by something this provider does not publish, which is
        the same as unsigned.
        """
        try:
            kid = str(jwt.get_unverified_header(token)["kid"])
        except Exception as exc:
            raise OidcError("token names no signing key") from exc
        if kid not in self._keys:
            # A refetch is an unauthenticated request an anonymous caller can
            # cause by inventing a key id, so it is rate limited rather than
            # offered once per attempt. A critic turned 25 forged tokens into 25
            # outbound fetches; the provider should not be reachable that way.
            now = time.monotonic()
            if now - self._last_key_fetch < KEY_REFETCH_COOLDOWN_SECONDS:
                raise OidcError("token was not signed by a key this provider publishes")
            self._last_key_fetch = now
            await self._fetch_keys(client)
        if kid not in self._keys:
            raise OidcError("token was not signed by a key this provider publishes")
        return self._keys[kid]

    async def authorization_url(
        self, client: httpx.AsyncClient, *, state: str, nonce: str, verifier: str
    ) -> str:
        document = await self.document(client)
        query = httpx.QueryParams(
            {
                "response_type": "code",
                "client_id": self.settings.client_id,
                "redirect_uri": self.settings.redirect_uri,
                "scope": " ".join(self.settings.scopes),
                "state": state,
                "nonce": nonce,
                "code_challenge": challenge_for(verifier),
                "code_challenge_method": "S256",
            }
        )
        return f"{document['authorization_endpoint']}?{query}"

    async def exchange_code(
        self, client: httpx.AsyncClient, *, code: str, verifier: str
    ) -> dict[str, Any]:
        document = await self.document(client)
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.settings.redirect_uri,
            "client_id": self.settings.client_id,
            "code_verifier": verifier,
        }
        if self.settings.client_secret:
            form["client_secret"] = self.settings.client_secret
        response = await client.post(str(document["token_endpoint"]), data=form)
        if response.status_code != 200:
            # The provider's error body can name the code and the client. None of
            # that belongs in an answer to whoever just arrived at the callback.
            raise OidcError("the provider refused the authorization code")
        tokens: dict[str, Any] = response.json()
        if "id_token" not in tokens:
            raise OidcError("the provider returned no ID token")
        return tokens

    async def refresh_at_provider(
        self, client: httpx.AsyncClient, *, refresh_token: str
    ) -> dict[str, Any]:
        """Spend the provider's refresh token, which is how it gets to say no.

        This is the only thing that asks the provider whether the person is still
        who they were at login. Without it a session is decided once and never
        revisited, so somebody disabled, locked out, or password-reset upstream
        keeps working here until an absolute clock they cannot see runs out.

        A refusal is not an error to log and continue past. It is the provider
        declining to vouch for this person any more, and the caller ends the
        session on it.
        """
        document = await self.document(client)
        form = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self.settings.client_id,
        }
        if self.settings.client_secret:
            form["client_secret"] = self.settings.client_secret
        response = await client.post(str(document["token_endpoint"]), data=form)
        if response.status_code != 200:
            raise OidcError("the provider will no longer vouch for this session")
        tokens: dict[str, Any] = response.json()
        return tokens

    async def verify_id_token(
        self, client: httpx.AsyncClient, *, id_token: str, nonce: str
    ) -> dict[str, Any]:
        """Signature, issuer, audience, expiry, and the nonce this login started with."""
        key = await self.signing_key(client, id_token)
        try:
            claims: dict[str, Any] = jwt.decode(
                id_token,
                key,
                algorithms=list(ALLOWED_ALGORITHMS),
                audience=self.settings.client_id,
                issuer=self.settings.issuer,
                leeway=CLOCK_SKEW_SECONDS,
                options={"require": ["iss", "sub", "aud", "exp", "iat"]},
            )
        except OidcError:
            raise
        except Exception as exc:
            raise OidcError("the ID token did not verify") from exc
        # PyJWT is satisfied when `aud` *contains* our client id. OIDC Core
        # 3.1.3.7 is not: an ID token "MUST be rejected if it contains additional
        # audiences not trusted by the Client", and we trust none. Without this,
        # any co-tenant client at the same provider holding a multi-audience
        # token signs in here as its own subject. A critic proved exactly that.
        raw_audience = claims.get("aud")
        audiences = [raw_audience] if isinstance(raw_audience, str) else list(raw_audience or [])
        if any(audience != self.settings.client_id for audience in audiences):
            raise OidcError("the ID token is addressed to somebody else as well")
        # And where the provider names an authorized party, it has to be us.
        authorized_party = claims.get("azp")
        if authorized_party is not None and str(authorized_party) != self.settings.client_id:
            raise OidcError("the ID token was authorized for a different party")

        # The nonce is what ties this token to the browser that started this
        # login. Comparing it to the row rather than to the request is the point.
        if not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
            raise OidcError("the ID token belongs to a different login attempt")
        return claims

    async def verify_logout_token(
        self, client: httpx.AsyncClient, *, logout_token: str
    ) -> dict[str, Any]:
        """A back-channel logout token, per OpenID Connect Back-Channel Logout 1.0.

        The three checks that are easy to omit and each fatal: the ``events``
        claim has to actually carry the back-channel logout event, the token has
        to name a session or a subject, and it must **not** carry ``nonce`` — a
        logout token with one is an ID token being replayed as a logout.
        """
        key = await self.signing_key(client, logout_token)
        try:
            claims: dict[str, Any] = jwt.decode(
                logout_token,
                key,
                algorithms=list(ALLOWED_ALGORITHMS),
                audience=self.settings.client_id,
                issuer=self.settings.issuer,
                leeway=CLOCK_SKEW_SECONDS,
                options={"require": ["iss", "aud", "iat", "exp", "jti", "events"]},
            )
        except OidcError:
            raise
        except Exception as exc:
            raise OidcError("the logout token did not verify") from exc

        if "nonce" in claims:
            raise OidcError("a logout token must not carry a nonce")
        events = claims.get("events")
        if not isinstance(events, dict) or BACKCHANNEL_LOGOUT_EVENT not in events:
            raise OidcError("the logout token does not carry the back-channel logout event")
        if not claims.get("sub") and not claims.get("sid"):
            raise OidcError("the logout token names neither a session nor a subject")
        return claims

    async def end_session_url(
        self, client: httpx.AsyncClient, *, id_token: str | None, state: str, redirect_to: str
    ) -> str | None:
        """The provider's RP-initiated logout URL, when it publishes one.

        ``redirect_to`` has already been checked against the configured allowlist
        by the caller; a target that arrived in a request and was not on it never
        reaches here.
        """
        document = await self.document(client)
        endpoint = document.get("end_session_endpoint")
        if not endpoint:
            return None
        params: dict[str, str] = {"client_id": self.settings.client_id, "state": state}
        if id_token:
            params["id_token_hint"] = id_token
        if redirect_to:
            params["post_logout_redirect_uri"] = redirect_to
        return f"{endpoint}?{httpx.QueryParams(params)}"
