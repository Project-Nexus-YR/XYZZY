"""Bootstrap and demo seeding: migrations, backfills, and the seeded demo workspace."""

from __future__ import annotations

import logging
import re
from contextlib import suppress
from datetime import timedelta
from pathlib import Path
from typing import Any

from ..domain.models import (
    AgentTemplate,
    AgentTrigger,
    BranchMode,
    MessageRole,
    OutputDisposition,
    ParticipantType,
    RunSettlement,
    User,
    new_id,
    utcnow,
)
from ..security.audit import GENESIS_HASH, event_chain_hash
from ._shared import (
    DEMO_SECOND_USER_ID,
    DEMO_USER_ID,
    _SharedMixin,
)

log = logging.getLogger(__name__)


class _BootstrapMixin(_SharedMixin):
    """Mixin providing the bootstrap surface of MultiplayerService."""

    _EVENT_CHAIN_MIGRATION_NAME = "033_the_log_commits_to_its_past.sql"

    async def _backfill_event_chain(self) -> None:
        """Hash events written before the chain existed, room by room, in order.

        Only rows whose event_hash is NULL are touched, so a tampered stored
        hash is never papered over by a fresh recomputation. Runs only on the
        boot that applies the migration adding these columns: on every later
        boot, that migration is already on record, so a NULL event_hash found
        then is not a legacy row waiting on this method, it is tampering, and
        this method is not the one that gets to decide that quietly.
        """
        if not self._event_chain_migration_is_new:
            return
        rooms = await self.db.fetch_all(
            "SELECT DISTINCT room_id FROM room_events WHERE event_hash IS NULL"
        )
        for room_row in rooms:
            room_id = str(room_row["room_id"])
            async with self.db.transaction():
                rows = await self.db.fetch_all(
                    "SELECT event_id, sequence, event_type, payload, actor_id, actor_type, "
                    "timestamp, schema_version, event_hash "
                    "FROM room_events WHERE room_id = ? ORDER BY sequence",
                    (room_id,),
                )
                prev_hash = GENESIS_HASH
                for row in rows:
                    if row["event_hash"] is not None:
                        prev_hash = str(row["event_hash"])
                        continue
                    event_hash = event_chain_hash(
                        prev_hash,
                        str(row["event_id"]),
                        room_id,
                        int(row["sequence"]),
                        str(row["event_type"]),
                        str(row["payload"]),
                        str(row["actor_id"]),
                        str(row["actor_type"]),
                        str(row["timestamp"]),
                        int(row["schema_version"]),
                    )
                    await self.db.execute(
                        "UPDATE room_events SET prev_hash = ?, event_hash = ? WHERE event_id = ?",
                        (prev_hash, event_hash, str(row["event_id"])),
                    )
                    prev_hash = event_hash

    async def _apply_migrations(self, migrations_dir: Path) -> None:
        """Apply each pending migration, one self-contained transaction per file.

        Two processes booting the same fresh file used to each decide which
        migrations were pending from one read taken before either held any
        lock. The loser's own ``BEGIN IMMEDIATE`` for a file blocked it until
        the winner committed that same file, but the loser never re-checked
        after waiting: it replayed a migration the winner had just committed
        and failed on a ``UNIQUE`` violation (or an earlier "already exists"
        from the body itself) instead of finding nothing to do. The fix does
        not try to make the check-then-act atomic before the write — that
        would need one SQLite transaction spanning every pending file, and
        ``executescript`` (the only way to run a migration's multiple
        statements, some of them trigger bodies with their own embedded
        semicolons) commits whatever transaction is open the moment it runs,
        so no such span is possible with the tools SQLite's Python driver
        gives a caller. Instead, a file that fails to apply is re-checked
        against ``schema_migrations`` before being treated as a real failure:
        if it is there now, another process applied it while this one was
        waiting for the same lock, which is success by another name, not an
        error.

        A crash mid-migration leaves the database exactly at the previous one:
        the script's statements and its schema_migrations row are one
        transaction, so nothing half-applied is ever marked done.

        A migration that uses the sanctioned rebuild recipe declares
        ``PRAGMA foreign_keys=OFF``, which a transaction would silently ignore,
        so that toggle is hoisted onto the connection around the transaction.
        """
        await self.db.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        migration_files = sorted(migrations_dir.glob("*.sql"))
        bodies: dict[str, str] = {}
        for migration_file in migration_files:
            body = migration_file.read_text()
            # A body that commits inside the wrapper would leave its own DDL
            # committed but unrecorded on a later failure - wedged forever.
            # (A stray BEGIN needs no guard: it fails as a nested transaction
            # and rolls back cleanly.)
            if re.search(r"(?im)(?:^|;)\s*(COMMIT|ROLLBACK)\b", body):
                raise RuntimeError(f"migration {migration_file.name} manages its own transaction")
            bodies[migration_file.name] = body
        applied_rows = await self.db.fetch_all("SELECT name FROM schema_migrations")
        applied = {str(row["name"]) for row in applied_rows}
        # A name this checkout does not ship means a newer build already
        # migrated this database: opening it anyway would run old code against
        # triggers, columns and CHECK vocabularies it never saw, so refuse at
        # boot rather than fail later at request time on a write those
        # never-seen constraints reject.
        unknown = applied - set(bodies)
        if unknown:
            raise RuntimeError(
                "database was migrated by a newer build: this checkout does not "
                f"ship {sorted(unknown)}"
            )
        # Read before this boot applies anything: true only for the one boot
        # that is about to apply the event chain migration for the first time.
        self._event_chain_migration_is_new = self._EVENT_CHAIN_MIGRATION_NAME not in applied
        for migration_file in migration_files:
            if migration_file.name in applied:
                continue
            body = bodies[migration_file.name]
            # Case and whitespace insensitive, and blind to a comment mentioning
            # the pragma rather than issuing it: a substring match on the raw
            # body would miss a respelled pragma (extra spaces, lower case) and
            # would also fire on a comment that merely names the literal,
            # disabling FK enforcement for a migration that never asked for that.
            body_without_comments = re.sub(r"--[^\n]*", "", body)
            wants_foreign_keys_off = bool(
                re.search(
                    r"(?i)\bPRAGMA\s+foreign_keys\s*=\s*(OFF|0|false)\b", body_without_comments
                )
            )
            record = (
                "INSERT INTO schema_migrations(name, applied_at) VALUES "
                f"('{migration_file.name.replace(chr(39), chr(39) * 2)}', "
                f"'{utcnow().isoformat()}');"
            )
            if wants_foreign_keys_off:
                await self.db.execute("PRAGMA foreign_keys=OFF")
            try:
                if wants_foreign_keys_off:
                    # The SQLite rebuild recipe's own step 10: a rebuild that drops
                    # a referenced row or copies with a wrong column list commits
                    # an orphan and records itself as applied unless this is
                    # checked before the commit, at the one moment it is still
                    # reversible. Run inside the same transaction, so the check
                    # sees the rebuild's own uncommitted rows.
                    #
                    # Scoped to the tables this migration itself creates: a bare
                    # ``PRAGMA foreign_key_check`` inspects the whole database, and
                    # this product deliberately keeps rows an erasure or a legacy
                    # import left referencing something gone — a rebuild of some
                    # unrelated table must not be refused for an orphan it did not
                    # create and is not touching. The sanctioned recipe creates the
                    # rebuild under a temporary name and renames it over the
                    # original once the copy is done, so a table this script
                    # renames away is replaced here with what it was renamed to —
                    # the name that will actually exist once the script finishes.
                    rebuilt_tables = dict.fromkeys(
                        re.findall(
                            r"(?im)^\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
                            r"([A-Za-z_][A-Za-z0-9_]*)",
                            body_without_comments,
                        )
                    )
                    for old_name, new_name in re.findall(
                        r"(?im)^\s*ALTER\s+TABLE\s+([A-Za-z_][A-Za-z0-9_]*)\s+"
                        r"RENAME\s+TO\s+([A-Za-z_][A-Za-z0-9_]*)",
                        body_without_comments,
                    ):
                        if old_name in rebuilt_tables:
                            del rebuilt_tables[old_name]
                            rebuilt_tables[new_name] = None
                    await self.db.execute_script(f"BEGIN IMMEDIATE;\n{body}\n{record}")
                    violations: list[Any] = []
                    for table in rebuilt_tables:
                        cursor = await self.db.execute(f"PRAGMA foreign_key_check({table})")
                        violations.extend(await cursor.fetchall())
                    if violations:
                        raise RuntimeError(
                            f"migration {migration_file.name} left "
                            f"{len(violations)} foreign key violation(s)"
                        )
                    await self.db.execute("COMMIT")
                else:
                    await self.db.execute_script(f"BEGIN IMMEDIATE;\n{body}\n{record}\nCOMMIT;")
            except Exception as exc:
                with suppress(Exception):
                    await self.db.execute("ROLLBACK")
                # This connection's own BEGIN IMMEDIATE blocked until a competing
                # process's identical pass committed this very file, and it woke
                # up still holding a decision made before that wait. Asking again
                # now is what tells "somebody else just finished this" apart from
                # an actual failure in the body.
                recorded = await self.db.fetch_one(
                    "SELECT 1 FROM schema_migrations WHERE name = ?", (migration_file.name,)
                )
                if recorded is not None:
                    continue
                raise RuntimeError(f"migration {migration_file.name} failed") from exc
            finally:
                if wants_foreign_keys_off:
                    await self.db.execute("PRAGMA foreign_keys=ON")

    async def _settle_orphaned_mention_runs(self) -> None:
        """Settle mention runs whose dispatcher died before it could claim them.

        A mention run is committed PENDING and claimed by its dispatcher immediately
        after that commit. A process that dies in between leaves a run nothing will
        ever pick up, and only that run is an orphan: a claimed run belongs to a
        dispatcher that is working on it, and settling it here would destroy healthy
        work another process is doing. The sweep therefore reads only unclaimed runs
        and writes conditionally, so it loses every race it enters rather than
        winning one it should not. Restarting the turn instead would replay a
        question the room has probably moved past; the author can address the agent
        again.
        """
        orphans = await self.repos.executions.list_unclaimed_pending_by_trigger(
            AgentTrigger.MENTION
        )
        for orphan in orphans:
            await self._settle_undispatched_run(
                orphan.execution_id,
                "dispatcher stopped before the run started",
                RunSettlement.ORPHANED,
            )

    async def _backfill_legacy_artifact_provenance_hashes(self) -> None:
        """Bind pre-migration snapshots using the best evidence available at upgrade time."""
        versions = await self.repos.artifacts.list_versions_without_provenance_hash()
        for version in versions:
            claims = await self.repos.artifacts.get_version_provenance(version.version_id)
            provenance_hash = self._artifact_provenance_hash(version, claims)
            await self.repos.artifacts.set_provenance_hash_if_empty(
                version.version_id, provenance_hash
            )

    async def _backfill_participant_handles(self) -> None:
        """Address the participants who joined before handles existed.

        Rows arrive in a fixed order so that two rooms upgrading from the same state
        end up with the same handles, including which of two colliding names got the
        bare one.
        """
        for row in await self.repos.handles.list_participants_without_handles():
            await self._issue_handle(
                str(row["room_id"]),
                ParticipantType(str(row["participant_type"])),
                str(row["participant_id"]),
                str(row["display_name"]),
            )

    async def _seed_default_templates(self) -> None:
        templates = await self.repos.agents.list_templates()
        if templates:
            return
        defaults = [
            AgentTemplate(
                template_id=new_id("tmpl"),
                name="Architect",
                description="Plans system architecture",
                role="Architect",
                system_prompt="You are an architect.",
                capabilities=frozenset({"planning", "analysis", "decision_making"}),
            ),
            AgentTemplate(
                template_id=new_id("tmpl"),
                name="Researcher",
                description="Investigates questions",
                role="Researcher",
                system_prompt="You are a researcher.",
                capabilities=frozenset({"research", "analysis", "retrieval"}),
            ),
            AgentTemplate(
                template_id=new_id("tmpl"),
                name="Coder",
                description="Writes and reviews code",
                role="Coder",
                system_prompt="You are a software engineer.",
                capabilities=frozenset({"coding", "testing", "review"}),
            ),
            AgentTemplate(
                template_id=new_id("tmpl"),
                name="Security Reviewer",
                description="Reviews for security issues",
                role="Security Reviewer",
                system_prompt="You are a security expert.",
                capabilities=frozenset({"security", "review", "analysis"}),
            ),
            AgentTemplate(
                template_id=new_id("tmpl"),
                name="Synthesizer",
                description="Combines multi-agent outputs",
                role="Synthesizer",
                system_prompt="You are a synthesizer.",
                capabilities=frozenset({"synthesis", "writing", "analysis"}),
            ),
        ]
        for t in defaults:
            await self.repos.agents.create_template(t)

    async def seed_demo_workspace(self) -> None:
        """Populate an empty demo deployment with one realistic, offline scene.

        Guarded on organizations existing at all, not on a flag row: a database
        that already has a workspace was seeded by an earlier startup of this
        same demo, or holds a real one, and either way there is nothing left
        for this call to add. That makes the guard the idempotence itself —
        a second startup finds a non-empty table and returns immediately.
        Every write below goes through the same service methods an HTTP
        caller would use, so it picks up every invariant those methods
        enforce for free, and needs no API key: leaving model_provider and
        model_name unset resolves to the SIMULATED provider, same as any
        other room with no provider configured.
        """
        if await self.db.fetch_one("SELECT 1 FROM organizations LIMIT 1") is not None:
            return
        _org, _workspace, room = await self.bootstrap_user_workspace(
            DEMO_USER_ID, "Yasser", "General"
        )
        room_id = room.room_id
        if await self.repos.users.get(DEMO_SECOND_USER_ID) is None:
            await self.repos.users.create(
                User(
                    user_id=DEMO_SECOND_USER_ID,
                    display_name="Amira",
                    email=f"{DEMO_SECOND_USER_ID}@demo.local",
                )
            )
        await self.invite_room_member(room_id, DEMO_SECOND_USER_ID, "editor", DEMO_USER_ID)
        demo_third_user_id = "user_demo_third"
        if await self.repos.users.get(demo_third_user_id) is None:
            await self.repos.users.create(
                User(
                    user_id=demo_third_user_id,
                    display_name="Karim",
                    email=f"{demo_third_user_id}@demo.local",
                )
            )
        await self.invite_room_member(room_id, demo_third_user_id, "editor", DEMO_USER_ID)

        async def say(sender: str, content: str, parent_message_id: str | None = None) -> str:
            message = await self.send_message(
                room_id,
                MessageRole.HUMAN,
                sender,
                content,
                parent_message_id=parent_message_id,
                invoke_mentioned_agents=False,
            )
            return message.message_id

        m1 = await say(
            DEMO_USER_ID,
            "Morning - picking up the payments-provider decision. Stripe vs Adyen vs "
            "building on our bank's raw API.",
        )
        await say(
            DEMO_SECOND_USER_ID,
            "Finance wants an answer by Thursday. The contract renewal is the forcing function.",
        )
        m3 = await say(
            DEMO_USER_ID,
            "Main unknowns for me: EU settlement times, and what the migration costs us "
            "in engineering weeks.",
        )
        await say(
            DEMO_SECOND_USER_ID,
            "I'll pull our current chargeback numbers so the branches have real inputs.",
        )
        await say(
            DEMO_USER_ID,
            "Adyen quotes T+1 for EU settlement on their site - worth verifying in the branch run.",
            parent_message_id=m3,
        )
        await say(
            DEMO_SECOND_USER_ID,
            "Our bank's API settles T+2 at best, and that's before reconciliation.",
            parent_message_id=m3,
        )
        await say(
            demo_third_user_id,
            "Watching from the finance side. Ping me once the branch has numbers, "
            "I'll sanity-check them against last quarter's chargeback report.",
        )
        await self.add_reaction(m1, DEMO_SECOND_USER_ID, "\U0001f44d")

        templates = (await self.list_agent_templates())[:2]
        agent_ids = []
        for template in templates:
            agent = await self.spawn_agent(
                room_id,
                template.template_id,
                template.name,
                requested_by=DEMO_USER_ID,
                require_member=True,
            )
            agent_ids.append(agent.agent_id)
        branch, runs = await self.start_branch(
            room_id,
            BranchMode.PARALLEL,
            "Compare Stripe, Adyen, and our bank's raw API for EU card payments: "
            "settlement time, fees at our volume, and migration effort.",
            DEMO_USER_ID,
            agent_ids,
        )
        for run in runs:
            await self.execute_branch_run(branch.branch_id, run.execution_id, DEMO_USER_ID)
        # Every output must be decided before a synthesis can read the branch, and
        # each one is included here so the seeded brief has both perspectives in it.
        for output in await self.list_room_outputs(room_id):
            await self.select_output(
                room_id, output.output_id, OutputDisposition.INCLUDED, DEMO_USER_ID
            )
        await self.synthesize_branch_decision_brief(
            branch.branch_id, "Decision Brief", DEMO_USER_ID
        )
        await say(
            DEMO_SECOND_USER_ID,
            "Reading the brief now. The settlement-time claim needs a source before we commit.",
        )
        await say(DEMO_USER_ID, "Agreed - flagged it in Evidence. Let's decide Thursday morning.")

        # Seeding runs in one instant, which stamps every message with the same
        # minute and makes the scene read as the fixture it is. Spread the
        # message rows back across a plausible stretch of morning instead. Only
        # messages.created_at moves: room_events keep their true times, so the
        # hash chain over the event log is untouched and still verifies.
        rows = await self.db.fetch_all(
            "SELECT message_id FROM messages WHERE room_id = ? ORDER BY created_at, message_id",
            (room_id,),
        )
        gaps_minutes = [0, 4, 9, 2, 7, 3, 12, 5, 8, 6, 4, 10]
        start = utcnow() - timedelta(minutes=sum(gaps_minutes[: len(rows)]) + 3)
        elapsed = start
        for i, row in enumerate(rows):
            elapsed += timedelta(minutes=gaps_minutes[i % len(gaps_minutes)])
            await self.db.execute(
                "UPDATE messages SET created_at = ? WHERE message_id = ?",
                (elapsed.isoformat(), row["message_id"]),
            )
