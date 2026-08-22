"""Concurrency acceptance for the branch context-boundary/turn-lock race."""

from __future__ import annotations

import asyncio

import pytest

from multiplayer.db.connection import Database
from multiplayer.domain.models import (
    BranchMode,
    DomainError,
    Message,
    MessageRole,
    TurnLockScopeType,
)
from multiplayer.realtime.hub import RealtimeHub
from multiplayer.services.service import MultiplayerService


@pytest.mark.asyncio
async def test_racing_message_is_captured_before_boundary_or_rejected_after_lock() -> None:
    db = Database(":memory:")
    await db.connect()
    service = MultiplayerService(db, RealtimeHub())
    await service.initialize()
    try:
        org = await service.create_organization("Race org", "race-org", "owner")
        workspace = await service.create_workspace(org.org_id, "Main", "main", "owner")
        room = await service.create_room(workspace.workspace_id, "Race", "owner")
        template = (await service.list_agent_templates())[0]
        agent = await service.spawn_agent(room.room_id, template.template_id)
        gate = asyncio.Event()

        async def send_racing_message() -> Message | DomainError:
            await gate.wait()
            try:
                return await service.send_message(
                    room.room_id,
                    MessageRole.HUMAN,
                    "owner",
                    "Racing context fact",
                )
            except DomainError as exc:
                return exc

        async def start_locked_branch():
            await gate.wait()
            return await service.start_branch(
                room.room_id,
                BranchMode.TURN_LOCKED_SINGLE,
                "Make the turn-locked decision.",
                "owner",
                [agent.agent_id],
            )

        message_task = asyncio.create_task(send_racing_message())
        branch_task = asyncio.create_task(start_locked_branch())
        gate.set()
        message_result, (branch, runs) = await asyncio.gather(message_task, branch_task)

        captured_ids = set(branch.context_message_ids)
        snapshot_ids = {item["message_id"] for item in branch.context_snapshot["messages"]}
        if isinstance(message_result, Message):
            assert message_result.message_id in captured_ids
            assert message_result.message_id in snapshot_ids
            message_event = next(
                event
                for event in await service.get_room_events(room.room_id)
                if event.payload.get("message_id") == message_result.message_id
            )
            assert message_event.sequence <= branch.context_event_sequence
        else:
            assert "turn is locked" in str(message_result)
            assert all(
                item["content"] != "Racing context fact"
                for item in branch.context_snapshot["messages"]
            )
            assert all(
                message.content != "Racing context fact"
                for message in await service.list_room_messages(room.room_id)
            )

        with pytest.raises(DomainError, match="turn is locked"):
            await service.send_message(
                room.room_id, MessageRole.HUMAN, "owner", "Must wait for release"
            )
        await service.execute_branch_run(branch.branch_id, runs[0].execution_id)
        accepted = await service.send_message(
            room.room_id, MessageRole.HUMAN, "owner", "Accepted after terminal release"
        )
        assert accepted.content == "Accepted after terminal release"
        assert (
            await service.repos.turn_locks.get_active(
                scope_type=TurnLockScopeType.ROOM, scope_id=room.room_id
            )
            is None
        )
    finally:
        await db.close()
