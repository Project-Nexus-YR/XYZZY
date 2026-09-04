"""Core service layer: orchestrates domain operations across repos, events, and NEXUS.

The class itself is only the composition: construction and startup. Its
surface is provided by the mixins in this package, one module per domain
cluster (organizations and workspaces, rooms, agents, runs, agent turns,
branches and synthesis, conversation, room records, ontology, Meta, audit,
agent tasks, erasure, and bootstrap); ``_shared.py`` holds what more than one
of them needs. Splitting a 9,700 line class by import made every mixin's ``self``
untyped; ``_ServiceCore`` in ``_shared.py`` is what keeps ``mypy --strict``
able to check each one on its own.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from ..db.connection import Database
from ..db.repositories import Repos
from ..domain.models import DomainError as DomainError
from ..domain.models import new_id
from ..harness import NEXUS_HARNESS_ID as NEXUS_HARNESS_ID
from ..metrics import Metrics
from ..nexus_bridge.agent_bridge import NexusAgentBridge
from ..realtime.hub import RealtimeHub
from ..security.authorization import AuthorizationError as AuthorizationError
from ..security.authorization import RoomPolicy
from ..services.presence import PresenceService
from ._shared import (
    _ROOM_EVENTS_MAX_LIMIT as _ROOM_EVENTS_MAX_LIMIT,
)
from ._shared import (
    DEMO_SECOND_USER_ID as DEMO_SECOND_USER_ID,
)
from ._shared import (
    DEMO_USER_ID as DEMO_USER_ID,
)
from ._shared import (
    VALID_AGENT_TRANSITIONS as VALID_AGENT_TRANSITIONS,
)
from ._shared import (
    VALID_DECISION_TRANSITIONS as VALID_DECISION_TRANSITIONS,
)
from ._shared import (
    VALID_EXECUTION_TRANSITIONS as VALID_EXECUTION_TRANSITIONS,
)
from ._shared import (
    VALID_SESSION_TRANSITIONS as VALID_SESSION_TRANSITIONS,
)
from ._shared import (
    VALID_TASK_TRANSITIONS as VALID_TASK_TRANSITIONS,
)
from ._shared import (
    AgentLaunchRefused as AgentLaunchRefused,
)
from ._shared import (
    RunAuthorityRevoked as RunAuthorityRevoked,
)
from ._shared import (
    _TurnContinuation as _TurnContinuation,
)
from ._shared import (
    _validate_transition as _validate_transition,
)
from .agent_tasks import _AgentTasksMixin
from .agents import _AgentsMixin
from .audit import _AuditMixin
from .bootstrap import _BootstrapMixin
from .branches import _BranchesMixin
from .conversation import _ConversationMixin
from .erasure import _ErasureMixin
from .meta import _MetaMixin
from .ontology import _OntologyMixin
from .organizations import _OrganizationsMixin
from .records import _RecordsMixin
from .rooms import _RoomsMixin
from .runs import _RunsMixin
from .steps import _StepsMixin


class MultiplayerService(
    _OrganizationsMixin,
    _RoomsMixin,
    _AgentsMixin,
    _RunsMixin,
    _StepsMixin,
    _BranchesMixin,
    _ConversationMixin,
    _RecordsMixin,
    _OntologyMixin,
    _MetaMixin,
    _AuditMixin,
    _AgentTasksMixin,
    _ErasureMixin,
    _BootstrapMixin,
):
    """Orchestrates domain operations across repos, realtime events, and NEXUS.

    Every operation lives in one of the mixins above, grouped by the domain
    cluster it belongs to. This class supplies the one thing they all need to
    be one service instead of thirteen: a single ``__init__`` and startup.
    """

    def __init__(
        self,
        db: Database,
        hub: RealtimeHub,
        known_users: frozenset[str] | None = None,
        presence_redis: Any | None = None,
        metrics: Metrics | None = None,
        nexus: NexusAgentBridge | None = None,
    ) -> None:
        self.db = db
        self.repos = Repos(db)
        self.hub = hub
        self.presence = PresenceService(redis_client=presence_redis)
        self.metrics = metrics
        self.nexus = nexus if nexus is not None else NexusAgentBridge(db_path=":memory:")
        self.authorization = RoomPolicy(self.repos)
        # Principals the server authenticates; an invitation must name one of them
        # or a user row that bootstrapping already created.
        self.known_users = known_users or frozenset()
        self._running_executions: dict[str, asyncio.Task[None]] = {}
        # Per-run bearer credentials in plaintext, held only until the harness that
        # will use them is opened. The durable row keeps a SHA-256 hash and nothing
        # else, so a credential never outlives the process that issued it.
        self._run_credentials: dict[str, str] = {}
        # Identifies this dispatcher's claims on runs, so another process can tell
        # a run somebody is dispatching from one nobody ever picked up.
        self._dispatch_claim = new_id("dispatch")
        # Holds a strong reference to every background dispatch this process has
        # scheduled, so the event loop cannot garbage-collect a task nobody is
        # awaiting out from under it mid-flight; the done callback below is what
        # lets each one go once it finishes.
        self._background_tasks: set[asyncio.Task[None]] = set()
        # Set for real by _apply_migrations, before _backfill_event_chain ever
        # reads it. False here is the safe default: a backfill that has not
        # been told this is the migration's first boot must not touch anything.
        self._event_chain_migration_is_new = False

    async def initialize(self) -> None:
        await self._apply_migrations(Path(__file__).parent.parent / "migrations")
        await self._backfill_event_chain()
        await self._backfill_legacy_artifact_provenance_hashes()
        await self._backfill_participant_handles()
        # Objects written before their kind joined the search allowlist.
        await self.repos.search.backfill()
        await self._seed_default_templates()
        await self._settle_orphaned_mention_runs()
        await self.sweep_expired_run_leases()
        # Constant-work recovery, same as the run-lease sweep above: a crash
        # between an A2A accept and the background dispatch it schedules is
        # the only way a task sits SUBMITTED past the staleness threshold, so
        # a restart heals it here rather than leaving it stranded forever.
        await self.sweep_stale_submitted_agent_tasks()
        # The other half of that recovery: a task a harder kill left WORKING
        # behind a run that sweep_expired_run_leases just settled (or that
        # settled some other way) is failed here too, so a restart is enough
        # even when nothing this process runs afterward will ever revisit it.
        await self.sweep_stranded_working_agent_tasks()
