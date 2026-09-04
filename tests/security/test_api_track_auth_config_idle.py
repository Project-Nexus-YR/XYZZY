"""GET /api/v1/auth/config's own docstring claims "nothing here acts on the
session", but it authenticates the cookie by calling `TokenAuthenticator.
authenticate`, which extends the session's idle clock on the way (finding 19).
A cross-site top-level navigation to this unauthenticated route would then
push the victim's idle expiry forward.

The fix threads `extend_idle=False` through `authenticate`; these tests pin
that flag at the `TokenAuthenticator` level, which is what the route now
calls with.
"""

from __future__ import annotations

import pytest

from multiplayer.security.auth import TokenAuthenticator

from .test_sso_session_lifecycle import _sign_in, wired  # noqa: F401


async def _idle_expires_at(db, session_id: str) -> str:
    row = await db.fetch_one(
        "SELECT idle_expires_at FROM user_sessions WHERE session_id = ?", (session_id,)
    )
    assert row is not None
    return str(row["idle_expires_at"])


@pytest.mark.asyncio
async def test_extend_idle_false_leaves_the_idle_clock_untouched(wired):  # noqa: F811
    sessions, idp, _, db = wired
    authenticator = TokenAuthenticator(db, sessions.note_used)
    issued = await _sign_in(sessions, idp)

    before = await _idle_expires_at(db, issued.session.session_id)
    await authenticator.authenticate(f"Bearer {issued.access_token}", extend_idle=False)
    after = await _idle_expires_at(db, issued.session.session_id)

    assert after == before


@pytest.mark.asyncio
async def test_default_authenticate_still_extends_the_idle_clock(wired):  # noqa: F811
    sessions, idp, _, db = wired
    authenticator = TokenAuthenticator(db, sessions.note_used)
    issued = await _sign_in(sessions, idp)

    before = await _idle_expires_at(db, issued.session.session_id)
    await authenticator.authenticate(f"Bearer {issued.access_token}")
    after = await _idle_expires_at(db, issued.session.session_id)

    assert after != before
