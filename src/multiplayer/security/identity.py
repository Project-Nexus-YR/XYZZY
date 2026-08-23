"""Per-run bearer credentials and the signed-challenge proof of agent identity.

The credential is the opaque-token discipline of :mod:`multiplayer.security.auth`
applied to a run: the workspace stores only a SHA-256 hash and compares with
``hmac.compare_digest``, so a stored row never yields a usable credential.

A signature proves authorship across a transport nobody controls. Only public keys
are stored, so no agent secret can leak through an audit view, and verification is
optional at import: without it a ``SIGNED_CHALLENGE`` identity cannot answer, which
refuses the launch rather than admitting it unproven.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

_CHALLENGE_BYTES = 32
_CREDENTIAL_BYTES = 32


def new_run_credential() -> str:
    """A fresh per-run bearer credential. Held by the harness, hashed in the row."""
    return secrets.token_urlsafe(_CREDENTIAL_BYTES)


def credential_hash(credential: str) -> str:
    return hashlib.sha256(credential.encode()).hexdigest()


def credential_matches(credential: str, stored_hash: str) -> bool:
    """Constant-time comparison; an empty stored hash never matches."""
    if not stored_hash or not credential:
        return False
    return hmac.compare_digest(credential_hash(credential), stored_hash)


def new_launch_challenge() -> bytes:
    return secrets.token_bytes(_CHALLENGE_BYTES)


def key_fingerprint(public_key: str) -> str:
    return hashlib.sha256(public_key.encode()).hexdigest()


def verify_challenge_answer(public_key: str, challenge: bytes, answer: bytes | None) -> bool:
    """Whether ``answer`` is this key's signature over ``challenge``. Deny by default."""
    if not public_key or not challenge or not answer:
        return False
    try:
        raw = base64.b64decode(public_key, validate=True)
    except ValueError:
        return False
    return _ed25519_verify(raw, challenge, answer)


def _ed25519_verify(raw_key: bytes, challenge: bytes, answer: bytes) -> bool:
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:  # pragma: no cover - only on installs without cryptography
        return False
    try:
        Ed25519PublicKey.from_public_bytes(raw_key).verify(answer, challenge)
    except (InvalidSignature, ValueError):
        return False
    return True
