"""Regression: an agent without a live identity may not launch, and the database says so.

An agent instance could open a run with nothing durable saying which agent process it
was. Identity is now one immutable row per instance, revoked once rather than per run,
and the refusal is a BEFORE INSERT trigger on agent_runs rather than only a service
check — so a future code path that forgets the checker still cannot launch an anonymous
agent.

Identity is a gate, never a term: it can refuse earlier, and it can never widen the
five-way intersection.
"""

from __future__ import annotations

import base64
import sqlite3
from typing import Any

import pytest

import multiplayer.nexus_bridge.agent_bridge as bridge_module
from multiplayer.db.connection import Database
from multiplayer.domain.models import (
    AgentIdentity,
    Execution,
    HarnessState,
    MessageRole,
    ProofMode,
    RunSettlement,
    new_id,
)
from multiplayer.harness import HarnessInfo, RunContext, SessionHandle
from multiplayer.harness.protocol import PROTOCOL_VERSION
from multiplayer.nexus_bridge.agent_bridge import NexusAgentBridge
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.security.authorization import AuthorizationError
from multiplayer.security.identity import key_fingerprint
from multiplayer.services.service import MultiplayerService


class _FinishingProvider:
    async def acomplete(self, prompt: str, response_schema: dict[str, Any]) -> dict[str, Any]:
        del prompt, response_schema
        return {
            "action": "finish",
            "output": {"content": "assessed"},
            "provider_name": "test-model",
            "provider_model": "identity-test",
            "provider_response_id": "response_finish",
            "provider_evidence": "finished",
        }


@pytest.fixture
async def service(monkeypatch: pytest.MonkeyPatch) -> MultiplayerService:
    monkeypatch.setattr(bridge_module, "_HAS_NEXUS", False)
    db = Database(":memory:")
    await db.connect()
    svc = MultiplayerService(db, RealtimeHub(), known_users=frozenset({"owner", "teammate"}))
    await svc.initialize()
    svc.nexus = NexusAgentBridge(model_provider=_FinishingProvider())
    yield svc
    await db.close()


async def _room(svc: MultiplayerService) -> str:
    org = await svc.create_organization("Identity org", "ident-org", "owner")
    workspace = await svc.create_workspace(org.org_id, "Main", "main", "owner")
    room = await svc.create_room(workspace.workspace_id, "Decision", "owner")
    return room.room_id


async def _researcher(svc: MultiplayerService, room_id: str) -> str:
    templates = await svc.list_agent_templates()
    template_id = next(t.template_id for t in templates if t.name == "Researcher")
    agent = await svc.spawn_agent(room_id, template_id, name="Researcher", requested_by="owner")
    return agent.agent_id


async def _refusals(svc: MultiplayerService, room_id: str) -> list[dict[str, Any]]:
    return [
        event.payload
        for event in await svc.get_room_events(room_id)
        if event.event_type.value == "agent.launch.refused"
    ]


# ── An identity exists, once, per instance ───────────────────────────────────


@pytest.mark.asyncio
async def test_spawning_an_agent_registers_one_in_process_identity(
    service: MultiplayerService,
) -> None:
    svc = service
    room_id = await _room(svc)
    agent_id = await _researcher(svc, room_id)

    identity = await svc.get_agent_identity(agent_id)
    assert identity.proof_mode is ProofMode.IN_PROCESS
    # Public keys only, and no key at all where there is no untrusted transport.
    assert identity.public_key is None
    assert identity.revoked_at is None
    types = [event.event_type.value for event in await svc.get_room_events(room_id)]
    assert "agent.identity.registered" in types


@pytest.mark.asyncio
async def test_an_instance_may_not_hold_two_identities(service: MultiplayerService) -> None:
    svc = service
    room_id = await _room(svc)
    agent_id = await _researcher(svc, room_id)

    with pytest.raises(sqlite3.IntegrityError):
        await svc.db.execute(
            "INSERT INTO agent_identities(identity_id, created_at, proof_mode, agent_id) "
            "VALUES (?, ?, 'IN_PROCESS', ?)",
            (new_id("ident"), "2026-01-01T00:00:00+00:00", agent_id),
        )


@pytest.mark.asyncio
async def test_a_key_exists_exactly_when_the_mode_says_there_is_a_boundary(
    service: MultiplayerService,
) -> None:
    """The CHECK is the argument: no keyless signed mode, no key without one."""
    svc = service
    room_id = await _room(svc)
    agent_id = await _researcher(svc, room_id)
    await svc.db.execute("DELETE FROM agent_identities WHERE agent_id = ?", (agent_id,))

    with pytest.raises(sqlite3.IntegrityError):
        await svc.db.execute(
            "INSERT INTO agent_identities(identity_id, created_at, proof_mode, agent_id) "
            "VALUES (?, ?, 'SIGNED_CHALLENGE', ?)",
            (new_id("ident"), "2026-01-01T00:00:00+00:00", agent_id),
        )
    with pytest.raises(sqlite3.IntegrityError):
        await svc.db.execute(
            "INSERT INTO agent_identities(identity_id, created_at, proof_mode, public_key, "
            "agent_id) VALUES (?, ?, 'IN_PROCESS', 'a-key', ?)",
            (new_id("ident"), "2026-01-01T00:00:00+00:00", agent_id),
        )


# ── Fail-closed launch, in the service and below it ──────────────────────────


@pytest.mark.asyncio
async def test_an_agent_with_no_identity_cannot_launch_through_the_service(
    service: MultiplayerService,
) -> None:
    svc = service
    room_id = await _room(svc)
    agent_id = await _researcher(svc, room_id)
    await svc.db.execute("DELETE FROM agent_identities WHERE agent_id = ?", (agent_id,))

    with pytest.raises(AuthorizationError):
        await svc.send_message(
            room_id,
            MessageRole.HUMAN,
            "owner",
            "@Researcher please assess this",
            invoke_mentioned_agents=True,
        )

    assert await svc.db.fetch_all("SELECT run_id FROM agent_runs") == []
    assert await svc.repos.executions.list_by_room(room_id) == []
    # The message rolled back with the turn it asked for; the refusal did not.
    assert await svc.list_room_messages(room_id) == []
    assert [payload["reason"] for payload in await _refusals(svc, room_id)] == ["no_identity"]


@pytest.mark.asyncio
async def test_a_direct_repository_insert_is_refused_by_the_database(
    service: MultiplayerService,
) -> None:
    """Not only a service check: the trigger is what a forgotten checker runs into."""
    svc = service
    room_id = await _room(svc)
    agent_id = await _researcher(svc, room_id)
    identity = await svc.get_agent_identity(agent_id)
    session = await svc.start_agent_session(room_id, agent_id)
    execution = await svc.repos.executions.create(
        Execution(
            execution_id=new_id("exec"),
            session_id=session.session_id,
            agent_id=agent_id,
            authorized_by="owner",
        )
    )
    await svc.db.execute("DELETE FROM agent_identities WHERE agent_id = ?", (agent_id,))

    with pytest.raises(sqlite3.IntegrityError, match="live identity"):
        await svc.db.execute(
            "INSERT INTO agent_runs(run_id, execution_id, agent_id, identity_id, room_id, "
            "authorized_by, acting_user_id, harness_id, credential_hash, harness_state, "
            "lease_expires_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'owner', 'owner', 'nexus', 'x', 'STARTING', ?, ?)",
            (
                new_id("arun"),
                execution.execution_id,
                agent_id,
                identity.identity_id,
                room_id,
                "2099-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )
    assert await svc.db.fetch_all("SELECT run_id FROM agent_runs") == []


@pytest.mark.asyncio
async def test_a_revoked_identity_refuses_every_later_run(service: MultiplayerService) -> None:
    """Revoked once, not per run: nothing this agent does afterwards launches."""
    svc = service
    room_id = await _room(svc)
    agent_id = await _researcher(svc, room_id)
    await svc.send_message(
        room_id, MessageRole.HUMAN, "owner", "@Researcher assess", invoke_mentioned_agents=True
    )
    assert len(await svc.repos.executions.list_by_room(room_id)) == 1

    await svc.revoke_agent_identity(agent_id, "owner")

    with pytest.raises(AuthorizationError):
        await svc.send_message(
            room_id, MessageRole.HUMAN, "owner", "@Researcher again", invoke_mentioned_agents=True
        )
    assert len(await svc.repos.executions.list_by_room(room_id)) == 1
    assert [payload["reason"] for payload in await _refusals(svc, room_id)] == ["revoked"]
    types = [event.event_type.value for event in await svc.get_room_events(room_id)]
    assert "agent.identity.revoked" in types


@pytest.mark.asyncio
async def test_an_unknown_harness_refuses_to_launch(service: MultiplayerService) -> None:
    svc = service
    room_id = await _room(svc)
    agent_id = await _researcher(svc, room_id)
    await svc.db.execute(
        "UPDATE agent_instances SET harness_id = 'harness-from-nowhere' WHERE agent_id = ?",
        (agent_id,),
    )

    with pytest.raises(AuthorizationError):
        await svc.send_message(
            room_id, MessageRole.HUMAN, "owner", "@Researcher assess", invoke_mentioned_agents=True
        )
    assert await svc.db.fetch_all("SELECT run_id FROM agent_runs") == []
    assert [payload["reason"] for payload in await _refusals(svc, room_id)] == ["unknown_harness"]


# ── The signed-challenge mode, against a fixture ─────────────────────────────


def _signed_challenge_fixture() -> tuple[str, Any]:
    """A keypair for the mode; no production path builds one of these yet."""
    ed25519 = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ed25519")
    private = ed25519.Ed25519PrivateKey.generate()
    serialization = pytest.importorskip("cryptography.hazmat.primitives.serialization")
    raw = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(raw).decode(), private


class _SigningHarness:
    """A harness that holds the private half, as an out-of-process one would."""

    harness_id = "nexus"

    def __init__(self, private: Any, inner: Any) -> None:
        self._private = private
        self._inner = inner

    async def initialize(self, challenge: bytes | None) -> tuple[HarnessInfo, bytes | None]:
        info = HarnessInfo(self.harness_id, PROTOCOL_VERSION, frozenset())
        return info, (None if challenge is None else self._private.sign(challenge))

    async def session_new(self, run: RunContext) -> Any:
        return await self._inner.session_new(run)

    async def session_prompt(self, request: Any, on_update: Any) -> Any:
        return await self._inner.session_prompt(request, on_update)

    async def session_cancel(self, handle: SessionHandle, reason: str) -> None:
        await self._inner.session_cancel(handle, reason)


async def _make_signed(svc: MultiplayerService, agent_id: str, public_key: str) -> None:
    await svc.db.execute("DELETE FROM agent_identities WHERE agent_id = ?", (agent_id,))
    async with svc.db.transaction():
        await svc.repos.agent_identities.create_in_transaction(
            AgentIdentity(
                identity_id=new_id("ident"),
                agent_id=agent_id,
                proof_mode=ProofMode.SIGNED_CHALLENGE,
                public_key=public_key,
                key_fingerprint=key_fingerprint(public_key),
            )
        )


@pytest.mark.asyncio
async def test_a_signed_challenge_agent_launches_only_when_it_answers(
    service: MultiplayerService, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = service
    room_id = await _room(svc)
    agent_id = await _researcher(svc, room_id)
    public_key, private = _signed_challenge_fixture()
    await _make_signed(svc, agent_id, public_key)
    real_harness = svc._harness

    monkeypatch.setattr(
        svc, "_harness", lambda harness_id: _SigningHarness(private, real_harness(harness_id))
    )
    await svc.send_message(
        room_id, MessageRole.HUMAN, "owner", "@Researcher assess", invoke_mentioned_agents=True
    )

    runs = await svc.db.fetch_all("SELECT challenge_verified_at FROM agent_runs")
    assert len(runs) == 1
    assert runs[0]["challenge_verified_at"] is not None


@pytest.mark.asyncio
async def test_the_wrong_key_does_not_launch(
    service: MultiplayerService, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc = service
    room_id = await _room(svc)
    agent_id = await _researcher(svc, room_id)
    public_key, _ = _signed_challenge_fixture()
    _, other_private = _signed_challenge_fixture()
    await _make_signed(svc, agent_id, public_key)
    real_harness = svc._harness

    monkeypatch.setattr(
        svc,
        "_harness",
        lambda harness_id: _SigningHarness(other_private, real_harness(harness_id)),
    )

    with pytest.raises(AuthorizationError):
        await svc.send_message(
            room_id, MessageRole.HUMAN, "owner", "@Researcher assess", invoke_mentioned_agents=True
        )
    assert await svc.db.fetch_all("SELECT run_id FROM agent_runs") == []
    assert [payload["reason"] for payload in await _refusals(svc, room_id)] == ["challenge_failed"]


@pytest.mark.asyncio
async def test_an_unanswered_challenge_is_refused_by_the_database_too(
    service: MultiplayerService,
) -> None:
    """The service leg and the trigger leg both close; this one is the trigger."""
    svc = service
    room_id = await _room(svc)
    agent_id = await _researcher(svc, room_id)
    public_key, _ = _signed_challenge_fixture()
    session = await svc.start_agent_session(room_id, agent_id)
    execution = await svc.repos.executions.create(
        Execution(
            execution_id=new_id("exec"),
            session_id=session.session_id,
            agent_id=agent_id,
            authorized_by="owner",
        )
    )
    await _make_signed(svc, agent_id, public_key)
    identity = await svc.get_agent_identity(agent_id)

    with pytest.raises(sqlite3.IntegrityError, match="launch challenge"):
        await svc.db.execute(
            "INSERT INTO agent_runs(run_id, execution_id, agent_id, identity_id, room_id, "
            "authorized_by, acting_user_id, harness_id, credential_hash, harness_state, "
            "lease_expires_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'owner', 'owner', 'nexus', 'x', 'STARTING', ?, ?)",
            (
                new_id("arun"),
                execution.execution_id,
                agent_id,
                identity.identity_id,
                room_id,
                "2099-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )


# ── A run is an audit record ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_settled_run_is_terminal_and_never_rewritten(
    service: MultiplayerService,
) -> None:
    svc = service
    room_id = await _room(svc)
    agent_id = await _researcher(svc, room_id)
    await svc.send_message(
        room_id, MessageRole.HUMAN, "owner", "@Researcher assess", invoke_mentioned_agents=True
    )
    run = (await svc.db.fetch_all("SELECT * FROM agent_runs"))[0]
    assert run["harness_state"] == HarnessState.SETTLED.value
    assert run["settlement"] == RunSettlement.END_TURN.value

    with pytest.raises(sqlite3.IntegrityError, match="terminal"):
        await svc.db.execute(
            "UPDATE agent_runs SET settlement = 'CANCELLED' WHERE run_id = ?", (run["run_id"],)
        )
    with pytest.raises(sqlite3.IntegrityError, match="never deleted"):
        await svc.db.execute("DELETE FROM agent_runs WHERE run_id = ?", (run["run_id"],))
    # Deleting the instance would take the trail with it, so RESTRICT refuses that too.
    with pytest.raises(sqlite3.IntegrityError):
        await svc.db.execute("DELETE FROM agent_instances WHERE agent_id = ?", (agent_id,))


@pytest.mark.asyncio
async def test_a_run_may_not_be_repointed_at_another_agent(service: MultiplayerService) -> None:
    svc = service
    room_id = await _room(svc)
    await _researcher(svc, room_id)
    templates = await svc.list_agent_templates()
    other = await svc.spawn_agent(
        room_id,
        next(t.template_id for t in templates if t.name == "Architect"),
        name="Architect",
        requested_by="owner",
    )
    session = await svc.start_agent_session(room_id, other.agent_id)
    execution = await svc.start_execution(session.session_id, "owner")
    run = await svc.repos.agent_runs.get_by_execution(execution.execution_id)
    assert run is not None

    with pytest.raises(sqlite3.IntegrityError, match="re-pointed"):
        await svc.db.execute(
            "UPDATE agent_runs SET agent_id = ? WHERE run_id = ?", ("agent_elsewhere", run.run_id)
        )


@pytest.mark.asyncio
async def test_a_settled_row_must_carry_a_settlement(service: MultiplayerService) -> None:
    svc = service
    room_id = await _room(svc)
    agent_id = await _researcher(svc, room_id)
    session = await svc.start_agent_session(room_id, agent_id)
    execution = await svc.start_execution(session.session_id, "owner")
    run = await svc.repos.agent_runs.get_by_execution(execution.execution_id)
    assert run is not None

    # Settled with no settlement is terminal to the machine and invisible to the sweep.
    with pytest.raises(sqlite3.IntegrityError):
        await svc.db.execute(
            "UPDATE agent_runs SET harness_state = 'SETTLED' WHERE run_id = ?", (run.run_id,)
        )


# ── The rewrites that reopened a settled run ─────────────────────────────────

_RUN_COLUMNS = (
    "run_id, execution_id, agent_id, identity_id, room_id, authorized_by, acting_user_id, "
    "harness_id, credential_hash, harness_state, settlement, lease_expires_at, created_at"
)
_RUN_PLACEHOLDERS = ", ".join("?" * 13)


async def _architect(svc: MultiplayerService, room_id: str) -> str:
    templates = await svc.list_agent_templates()
    agent = await svc.spawn_agent(
        room_id,
        next(t.template_id for t in templates if t.name == "Architect"),
        name="Architect",
        requested_by="owner",
    )
    return agent.agent_id


async def _settled_run(svc: MultiplayerService, room_id: str) -> dict[str, Any]:
    await svc.send_message(
        room_id, MessageRole.HUMAN, "owner", "@Researcher assess", invoke_mentioned_agents=True
    )
    run = (await svc.db.fetch_all("SELECT * FROM agent_runs"))[0]
    assert run["harness_state"] == HarnessState.SETTLED.value
    return run


@pytest.mark.asyncio
async def test_a_settled_run_cannot_be_reopened_by_replacing_it(
    service: MultiplayerService,
) -> None:
    """INSERT OR REPLACE removes the conflicting row without firing the delete
    trigger, because recursive_triggers is off, so the settled run reopened as
    STREAMING under another agent. Refusing the duplicate insert is what sees it —
    the guard migration 018 already added to executions for this same bypass.
    """
    svc = service
    room_id = await _room(svc)
    agent_id = await _researcher(svc, room_id)
    run = await _settled_run(svc, room_id)
    other_id = await _architect(svc, room_id)
    other_identity = await svc.get_agent_identity(other_id)

    # The identity belongs to the agent it names, so the live-identity guard is
    # satisfied and the refusals below are the duplicate-insert guard, not that one.
    reopened = (
        run["run_id"],
        run["execution_id"],
        other_id,
        other_identity.identity_id,
        room_id,
        "owner",
        "mallory",
        "nexus",
        "x",
        HarnessState.STREAMING.value,
        None,
        "2099-01-01T00:00:00+00:00",
        "2026-01-01T00:00:00+00:00",
    )
    for verb in ("INSERT OR REPLACE INTO", "REPLACE INTO"):
        with pytest.raises(sqlite3.IntegrityError, match="never rewritten"):
            await svc.db.execute(
                f"{verb} agent_runs({_RUN_COLUMNS}) VALUES ({_RUN_PLACEHOLDERS})", reopened
            )
    # A fresh run_id aimed at the settled run's execution launders it just as well.
    with pytest.raises(sqlite3.IntegrityError, match="never rewritten"):
        await svc.db.execute(
            f"INSERT OR REPLACE INTO agent_runs({_RUN_COLUMNS}) VALUES ({_RUN_PLACEHOLDERS})",
            (new_id("arun"), *reopened[1:]),
        )

    settled = await svc.repos.agent_runs.get(run["run_id"])
    assert settled is not None
    assert settled.harness_state is HarnessState.SETTLED
    assert settled.settlement is RunSettlement.END_TURN
    assert settled.agent_id == agent_id
    assert len(await svc.db.fetch_all("SELECT run_id FROM agent_runs")) == 1


@pytest.mark.asyncio
async def test_a_settled_run_cannot_be_replaced_out_from_under_an_update(
    service: MultiplayerService,
) -> None:
    """UPDATE OR REPLACE resolves its conflict the same silent way, and its UPDATE
    triggers fire on the open row being moved rather than on the settled row it
    displaces. A run's own two keys are frozen so nothing can be aimed at them.
    """
    svc = service
    room_id = await _room(svc)
    await _researcher(svc, room_id)
    settled = await _settled_run(svc, room_id)
    other_id = await _architect(svc, room_id)
    session = await svc.start_agent_session(room_id, other_id)
    execution = await svc.start_execution(session.session_id, "owner")
    open_run = await svc.repos.agent_runs.get_by_execution(execution.execution_id)
    assert open_run is not None
    assert open_run.harness_state is not HarnessState.SETTLED

    with pytest.raises(sqlite3.IntegrityError, match="never rewritten"):
        await svc.db.execute(
            "UPDATE OR REPLACE agent_runs SET execution_id = ? WHERE run_id = ?",
            (settled["execution_id"], open_run.run_id),
        )
    with pytest.raises(sqlite3.IntegrityError, match="never rewritten"):
        await svc.db.execute(
            "UPDATE OR REPLACE agent_runs SET run_id = ? WHERE run_id = ?",
            (settled["run_id"], open_run.run_id),
        )

    still = await svc.repos.agent_runs.get(settled["run_id"])
    assert still is not None
    assert still.harness_state is HarnessState.SETTLED
    assert still.settlement is RunSettlement.END_TURN
    assert still.execution_id == settled["execution_id"]


# ── The identity row, immutable in the direction that matters ────────────────


@pytest.mark.asyncio
async def test_a_revoked_identity_is_never_restored(service: MultiplayerService) -> None:
    """A plain UPDATE cleared revoked_at and the agent launched again. Clearing it,
    moving it, deleting the row, replacing the row, and dropping the instance so the
    CASCADE takes the row without firing its trigger are all the same laundering.
    """
    svc = service
    room_id = await _room(svc)
    agent_id = await _researcher(svc, room_id)
    await svc.revoke_agent_identity(agent_id, "owner")
    identity = await svc.get_agent_identity(agent_id)
    assert identity.revoked_at is not None
    # This agent never ran, so the refusals below are the new guards rather than the
    # ON DELETE RESTRICT that an existing run would contribute.
    assert await svc.db.fetch_all("SELECT run_id FROM agent_runs") == []

    with pytest.raises(sqlite3.IntegrityError, match="never restored"):
        await svc.db.execute(
            "UPDATE agent_identities SET revoked_at = NULL WHERE agent_id = ?", (agent_id,)
        )
    with pytest.raises(sqlite3.IntegrityError, match="never restored"):
        await svc.db.execute(
            "UPDATE agent_identities SET revoked_at = ? WHERE agent_id = ?",
            ("2020-01-01T00:00:00+00:00", agent_id),
        )
    with pytest.raises(sqlite3.IntegrityError, match="never deleted"):
        await svc.db.execute("DELETE FROM agent_identities WHERE agent_id = ?", (agent_id,))
    with pytest.raises(sqlite3.IntegrityError, match="settled when it is written"):
        await svc.db.execute(
            "INSERT OR REPLACE INTO agent_identities(identity_id, created_at, revoked_at, "
            "proof_mode, agent_id) VALUES (?, ?, NULL, 'IN_PROCESS', ?)",
            (identity.identity_id, "2026-01-01T00:00:00+00:00", agent_id),
        )
    with pytest.raises(sqlite3.IntegrityError, match="never deleted"):
        await svc.db.execute("DELETE FROM agent_instances WHERE agent_id = ?", (agent_id,))

    assert (await svc.get_agent_identity(agent_id)).revoked_at == identity.revoked_at
    with pytest.raises(AuthorizationError):
        await svc.send_message(
            room_id,
            MessageRole.HUMAN,
            "owner",
            "@Researcher again",
            invoke_mentioned_agents=True,
        )


@pytest.mark.asyncio
async def test_a_signed_identity_may_not_be_downgraded_or_re_pointed(
    service: MultiplayerService,
) -> None:
    """The proof mode and the key are what a signed launch is checked against. An
    UPDATE that lowered the mode, swapped the key, or moved the row to another
    instance turned a launch the service had just refused into a permitted one.
    """
    svc = service
    room_id = await _room(svc)
    agent_id = await _researcher(svc, room_id)
    public_key, _ = _signed_challenge_fixture()
    await _make_signed(svc, agent_id, public_key)
    identity = await svc.get_agent_identity(agent_id)
    assert identity.proof_mode is ProofMode.SIGNED_CHALLENGE

    downgrades = (
        "UPDATE agent_identities SET proof_mode = 'IN_PROCESS', public_key = NULL, "
        "key_fingerprint = NULL WHERE agent_id = ?",
        "UPDATE agent_identities SET public_key = 'another-key' WHERE agent_id = ?",
        "UPDATE agent_identities SET key_fingerprint = 'another-print' WHERE agent_id = ?",
        "UPDATE agent_identities SET agent_id = 'agent_elsewhere' WHERE agent_id = ?",
        "UPDATE agent_identities SET identity_id = 'ident_elsewhere' WHERE agent_id = ?",
    )
    for statement in downgrades:
        with pytest.raises(sqlite3.IntegrityError, match="settled when it is written"):
            await svc.db.execute(statement, (agent_id,))

    with pytest.raises(sqlite3.IntegrityError, match="settled when it is written"):
        await svc.db.execute(
            "INSERT OR REPLACE INTO agent_identities(identity_id, created_at, proof_mode, "
            "agent_id) VALUES (?, ?, 'IN_PROCESS', ?)",
            (new_id("ident"), "2026-01-01T00:00:00+00:00", agent_id),
        )
    with pytest.raises(sqlite3.IntegrityError, match="never deleted"):
        await svc.db.execute("DELETE FROM agent_identities WHERE agent_id = ?", (agent_id,))
    with pytest.raises(sqlite3.IntegrityError, match="never deleted"):
        await svc.db.execute("DELETE FROM agent_instances WHERE agent_id = ?", (agent_id,))

    unchanged = await svc.get_agent_identity(agent_id)
    assert unchanged.proof_mode is ProofMode.SIGNED_CHALLENGE
    assert unchanged.public_key == public_key
    assert unchanged.identity_id == identity.identity_id
