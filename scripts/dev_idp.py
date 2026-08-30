#!/usr/bin/env python3
"""A throwaway identity provider for exercising XYZZY's browser sign-in locally.

Not a product, not a fixture library, not OIDC-compliant beyond the one flow
XYZZY's relying-party code (``multiplayer.security.oidc``) actually drives:
discovery, an authorization request that redirects straight back with a code
(there is no login form — you are the one user), a token exchange that mints a
real RS256-signed ID token, and the JWKS that verifies it. One user, one
process, one in-memory key generated fresh on every start.

Refuses to start unless its own issuer is a loopback host: this code trusts
whoever calls /authorize completely, which is only ever a reasonable thing
for a developer's own machine.

Usage:
    python scripts/dev_idp.py [--host 127.0.0.1] [--port 9100]

Then point XYZZY at it:
    XYZZY_OIDC_ISSUER=http://127.0.0.1:9100
    XYZZY_OIDC_CLIENT_ID=dev-client
    XYZZY_OIDC_REDIRECT_URI=http://127.0.0.1:8000/api/v1/auth/callback
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import os
import secrets
import sys
import time
from typing import Any
from urllib.parse import parse_qs

import jwt
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
KEY_ID = "dev-idp-key-1"
CODE_TTL_SECONDS = 120


def _challenge_for(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def create_app(*, issuer: str) -> FastAPI:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwks = {
        "keys": [
            jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
            | {"kid": KEY_ID, "use": "sig", "alg": "RS256"}
        ]
    }
    # code -> {nonce, code_challenge, redirect_uri, issued_at}. Dev-only, so a
    # process-lifetime dict is the whole store; a real provider never keeps a
    # code alive longer than one exchange, and neither does this one.
    pending_codes: dict[str, dict[str, Any]] = {}

    sub = os.environ.get("XYZZY_DEV_IDP_SUB", "dev-user")
    name = os.environ.get("XYZZY_DEV_IDP_NAME", "Dev User")
    email = os.environ.get("XYZZY_DEV_IDP_EMAIL", "dev@localhost")

    app = FastAPI(title="XYZZY dev IdP")

    @app.get("/.well-known/openid-configuration")
    async def discovery() -> dict[str, Any]:
        return {
            "issuer": issuer,
            "authorization_endpoint": f"{issuer}/authorize",
            "token_endpoint": f"{issuer}/token",
            "jwks_uri": f"{issuer}/jwks",
        }

    @app.get("/jwks")
    async def jwks_endpoint() -> dict[str, Any]:
        return jwks

    @app.get("/authorize")
    async def authorize(request: Request) -> RedirectResponse:
        params = request.query_params
        redirect_uri = params.get("redirect_uri", "")
        state = params.get("state", "")
        if not redirect_uri or not state:
            raise HTTPException(400, "redirect_uri and state are required")
        code = secrets.token_urlsafe(24)
        pending_codes[code] = {
            "nonce": params.get("nonce", ""),
            "code_challenge": params.get("code_challenge", ""),
            "issued_at": time.monotonic(),
        }
        return RedirectResponse(f"{redirect_uri}?code={code}&state={state}", status_code=307)

    @app.post("/token")
    async def token(request: Request) -> JSONResponse:
        # Parsed by hand rather than through FastAPI's Form(): that dependency
        # needs python-multipart, an extra dependency this throwaway script has
        # no reason to require.
        body = parse_qs((await request.body()).decode("utf-8", errors="replace"))
        grant_type = (body.get("grant_type") or [""])[0]
        code = (body.get("code") or [""])[0]
        code_verifier = (body.get("code_verifier") or [""])[0]
        refresh_token = (body.get("refresh_token") or [""])[0]

        if grant_type == "refresh_token":
            # No rotation to model here: the dev IdP has one user who never
            # gets disabled, so simply vouching for them again is enough to
            # keep XYZZY's own refresh-revalidation call satisfied.
            if not refresh_token:
                raise HTTPException(400, "refresh_token is required")
            return JSONResponse({"refresh_token": refresh_token})

        if grant_type != "authorization_code":
            raise HTTPException(400, "unsupported grant_type")
        attempt = pending_codes.pop(code, None)
        if attempt is None or time.monotonic() - attempt["issued_at"] > CODE_TTL_SECONDS:
            raise HTTPException(400, "unknown or expired code")
        if attempt["code_challenge"] and _challenge_for(code_verifier) != attempt["code_challenge"]:
            raise HTTPException(400, "code_verifier does not match the code_challenge")

        now = int(time.time())
        claims = {
            "iss": issuer,
            "sub": sub,
            "aud": os.environ.get("XYZZY_OIDC_CLIENT_ID", "dev-client"),
            "iat": now,
            "exp": now + 300,
            "nonce": attempt["nonce"],
            "sid": "dev-idp-session",
            "name": name,
            "email": email,
            "preferred_username": name,
        }
        id_token = jwt.encode(claims, key, algorithm="RS256", headers={"kid": KEY_ID})
        return JSONResponse(
            {
                "id_token": id_token,
                "refresh_token": secrets.token_urlsafe(24),
                "token_type": "Bearer",
            }
        )

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9100)
    args = parser.parse_args()

    if args.host not in LOOPBACK_HOSTS:
        print(
            f"refusing to start: {args.host!r} is not a loopback host. "
            "This provider trusts every caller and must never be reachable "
            "off the local machine.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    issuer = f"http://{args.host}:{args.port}"
    app = create_app(issuer=issuer)
    print(f"dev IdP issuer: {issuer}")
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
