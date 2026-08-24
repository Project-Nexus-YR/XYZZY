"""Actions that stay structurally outside the agent surface.

Policy can deny an action and still be one registry entry away from allowing
it. This module removes the choice instead: while a model-driven turn is
executing, the ambient context says so, and a fenced method refuses to run
at all - whoever called it, through whatever path. The fence keys on the
execution context rather than on any enumeration of call sites, so a tool
added next year inherits it without anyone remembering to say so.

Humans never trip it: a request that entered through the API on a human's
credential runs with no turn context set.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from .authorization import AuthorizationError

_agent_turn: ContextVar[str | None] = ContextVar("multiai_agent_turn", default=None)


@contextmanager
def agent_turn(execution_id: str) -> Iterator[None]:
    """Mark everything inside as running on a model's behalf."""
    token = _agent_turn.set(execution_id)
    try:
        yield
    finally:
        _agent_turn.reset(token)


def active_agent_turn() -> str | None:
    return _agent_turn.get()


def require_human_boundary(action: str) -> None:
    """Refuse an action that a model-driven turn is trying to reach."""
    execution_id = _agent_turn.get()
    if execution_id is not None:
        raise AuthorizationError(
            f"{action} is outside the agent surface (refused during turn {execution_id})"
        )
