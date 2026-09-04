"""Erasing a user: tombstoning their identity and redacting what they authored.

The room event log is hash chained and append only by design (see
``security/audit.py``): nothing here may rewrite a row's ``event_hash`` or
``prev_hash`` without breaking every hash after it. Erasing a user's content
therefore never edits a row's chained fields. It replaces the row's payload
with a marker, records what the marker replaced in ``event_redactions``, and
appends one ``EVENT_REDACTED`` event per room touched, so the erasure is
itself an attributed, chained fact rather than a silent edit.

A user row is never deleted, and neither is a room membership row: history
still needs a slot to say who was there. What is deleted is what identified
the person (display name, email, handle) and what they authored that a
person, not the server, put into it (message text, attachment names, titles).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Literal

from ..domain.events import EventType, RoomEvent
from ..domain.models import (
    DomainError,
    ParticipantType,
    RunSettlement,
    SearchObjectKind,
    new_id,
    utcnow,
)
from ..domain.synthesis import RESERVED_ARTIFACT_NAMES
from ._shared import _SharedMixin

log = logging.getLogger(__name__)

# Any of these keys carries something a person typed or named, never a fact the
# server alone derived from the act itself. A reaction's payload has none of
# them, which is exactly why a reaction is left alone.
#
# "instruction" was added in round 4: ``human_redirected_agent``'s payload
# (services/runs.py::intervene_execution) carries a human reviewer's steer text
# verbatim, the same shape as a message's "content". The durable copy this key
# also backs, ``execution_interventions.instruction``, was left as a
# deliberate exception in round 4; round 6 closed it (see
# ``_ABANDONED_INTERVENTION_REASON`` below) once migration 050 narrowed the
# column's immutability trigger.
_PERSONAL_PAYLOAD_KEYS = ("content", "body", "title", "filename", "instruction")

# name/description only count as personal on the one event type where they are
# always what the room's creator typed to open it. The same "name" key shows up
# on other human-actor events (an artifact's name, handled separately below)
# where it is not always personal, so it is not added to the generic set above.
_ROOM_METADATA_KEYS = ("name", "description")

_REDACTION_REASON = "user erasure"
# The event log's own actor for the one event an erasure appends per room. Not
# the erased user's id: an operator ran this from the CLI, not the user acting
# in the room, and the log's actor_id/actor_type pair has to say that plainly.
ERASURE_OPERATOR_ID = "system"

# A synthesis title, and any other field this track redacts that never rides
# inside a chained event payload, has no redaction_id to carry: nothing here
# backs it with an event_redactions row, so it gets a plain sentinel instead of
# the {"redacted": true, "redaction_id": ...} marker shape.
_NO_CHAIN_MARKER = "[redacted]"

# Round 5: a suspended turn parked at a reviewer is holding the erased human's
# own steer text (``suspended_turns.prompt``) with nobody left who can ever be
# asked to decide it. The row is discarded (not blanked in place) as part of
# settling the run it belongs to, and this is the reason recorded for why.
_ABANDONED_TURN_REASON = "the acting user was erased"

# Round 6: an execution whose only *unconsumed* intervention was the erased
# user's own is no longer waiting on anyone -- there is nobody left to ever
# steer it further, and settling it (rather than leaving it open) is the same
# move the suspended-turn sweep above already makes for the same shape of
# problem.
_ABANDONED_INTERVENTION_REASON = "the user who steered this run was erased"

# Round 4: every TEXT-affinity column this schema has, classified into exactly
# one of three buckets. ``tests/security/test_erasure_track_column_coverage.py``
# introspects the live, fully-migrated schema with ``PRAGMA table_info`` and
# asserts every (table, column) pair it finds is a key here — a column added
# later with no entry here fails that test outright, which is the point: this
# stops "did we redact everything" from ever again depending on a person
# remembering three-plus rounds of prior work.
#
# The detection rule that test uses: every table in ``sqlite_master`` except
# ``sqlite_%`` internals, FTS shadow tables (``%_fts``/``%_fts_%``), and
# ``schema_migrations`` (migration bookkeeping, not application data); every
# column of one of those tables whose ``PRAGMA table_info`` declared type is
# exactly ``TEXT``. Every other declared type in this schema (``INTEGER``,
# ``REAL``, ``BLOB``) is either an id/flag/timestamp/measurement or, in
# ``attachments.data``'s one case, the actual attachment bytes (handled by
# ``AttachmentRepo.erase_in_transaction``, which this classification does not
# need to separately track since it is never TEXT-affinity to begin with).
#
# The three buckets:
#   "redacted"          -- actively scrubbed by erasure code today: a message,
#                          a task/decision title, a branch's initiating_prompt,
#                          a room/artifact's name or description, an
#                          attachment's filename, a user's own identifying
#                          columns (tombstoned), a released handle (deleted,
#                          not marked, but the erased person's row is gone
#                          either way), a room_events payload carrying one of
#                          the personal keys above, an agent task's own
#                          asker-authored ``agent_task_messages.parts``
#                          (round 5, keyed by the human-opened task's
#                          TASK_DELEGATED events), a suspended turn's
#                          ``prompt`` (round 5: the row is discarded whole as
#                          part of settling the run it belongs to, which is
#                          also "redacted" the same way a released handle is:
#                          gone, not marked), or an execution intervention's
#                          ``instruction`` (round 6: migration 050 narrowed
#                          the immutability trigger 018/020 gave the column,
#                          so it is now scrubbed in place the same as a
#                          message, alongside the ``human_redirected_agent``
#                          event payload that already carried the same text).
#   "kept_by_ruling"    -- deliberately left alone, with a documented reason:
#                          live shared infrastructure (agent/room templates,
#                          a spawned agent's own name/system_prompt, an org's
#                          or workspace's name/slug -- shared configuration
#                          naming the group, not the person, round 5 ruling),
#                          frozen provenance evidence (artifact version
#                          content, everything under migration 003's
#                          append-only tables), or an explicitly immutable
#                          audit trail (an ontology review, migration 006 --
#                          an execution intervention's instruction was filed
#                          here through round 5, but migration 050 narrowed
#                          its trigger and round 6 moved it to "redacted"
#                          above). Round 4 filed the org/workspace name/slug
#                          pair here as a real,
#                          unresolved gap rather than a ruling, since neither
#                          table had a way to attribute authorship at all;
#                          round 5 turned it into an actual ruling instead
#                          (see SECURITY.md's Data Lifecycle section for the
#                          reasoning). See the round 3 through 5 reports for
#                          the full reasoning behind every entry here.
#   "not_user_authored" -- ids, hashes, enums, statuses, timestamps, and
#                          structural JSON that never carries free prose a
#                          person typed, even where its declared type is TEXT.
_ColumnClassification = Literal["redacted", "kept_by_ruling", "not_user_authored"]
_COLUMN_CLASSIFICATION: dict[tuple[str, str], _ColumnClassification] = {
    ("agent_address_allowlist", "agent_id"): "not_user_authored",
    ("agent_address_allowlist", "user_id"): "not_user_authored",
    ("agent_address_allowlist", "added_by"): "not_user_authored",
    ("agent_address_allowlist", "created_at"): "not_user_authored",
    ("agent_addressing", "agent_id"): "not_user_authored",
    ("agent_addressing", "room_id"): "not_user_authored",
    ("agent_addressing", "mode"): "not_user_authored",
    ("agent_addressing", "owner_user_id"): "not_user_authored",
    ("agent_addressing", "updated_at"): "not_user_authored",
    ("agent_addressing", "updated_by"): "not_user_authored",
    ("agent_identities", "identity_id"): "not_user_authored",
    ("agent_identities", "created_at"): "not_user_authored",
    ("agent_identities", "revoked_at"): "not_user_authored",
    ("agent_identities", "proof_mode"): "not_user_authored",
    ("agent_identities", "public_key"): "not_user_authored",
    ("agent_identities", "key_fingerprint"): "not_user_authored",
    ("agent_identities", "agent_id"): "not_user_authored",
    ("agent_instances", "agent_id"): "not_user_authored",
    ("agent_instances", "template_id"): "not_user_authored",
    ("agent_instances", "room_id"): "not_user_authored",
    ("agent_instances", "name"): "kept_by_ruling",
    ("agent_instances", "role"): "not_user_authored",
    ("agent_instances", "status"): "not_user_authored",
    ("agent_instances", "system_prompt"): "kept_by_ruling",
    ("agent_instances", "capabilities"): "not_user_authored",
    ("agent_instances", "model_provider"): "not_user_authored",
    ("agent_instances", "model_name"): "not_user_authored",
    ("agent_instances", "created_at"): "not_user_authored",
    ("agent_instances", "harness_id"): "not_user_authored",
    ("agent_outputs", "output_id"): "not_user_authored",
    ("agent_outputs", "room_id"): "not_user_authored",
    ("agent_outputs", "session_id"): "not_user_authored",
    ("agent_outputs", "execution_id"): "not_user_authored",
    ("agent_outputs", "agent_id"): "not_user_authored",
    ("agent_outputs", "content"): "kept_by_ruling",
    ("agent_outputs", "output_data"): "not_user_authored",
    ("agent_outputs", "source_prompt"): "kept_by_ruling",
    ("agent_outputs", "created_at"): "not_user_authored",
    ("agent_outputs", "provider_input"): "kept_by_ruling",
    ("agent_outputs", "provider_name"): "not_user_authored",
    ("agent_outputs", "provider_model"): "not_user_authored",
    ("agent_outputs", "provider_response_id"): "not_user_authored",
    ("agent_outputs", "provider_interventions"): "kept_by_ruling",
    ("agent_outputs", "provider_evidence"): "kept_by_ruling",
    ("agent_room_memberships", "membership_id"): "not_user_authored",
    ("agent_room_memberships", "agent_id"): "not_user_authored",
    ("agent_room_memberships", "room_id"): "not_user_authored",
    ("agent_room_memberships", "joined_at"): "not_user_authored",
    ("agent_room_memberships", "removed_at"): "not_user_authored",
    ("agent_room_memberships", "rejoined_from_membership_id"): "not_user_authored",
    ("agent_runs", "run_id"): "not_user_authored",
    ("agent_runs", "execution_id"): "not_user_authored",
    ("agent_runs", "agent_id"): "not_user_authored",
    ("agent_runs", "identity_id"): "not_user_authored",
    ("agent_runs", "room_id"): "not_user_authored",
    ("agent_runs", "authorized_by"): "not_user_authored",
    ("agent_runs", "acting_user_id"): "not_user_authored",
    ("agent_runs", "harness_id"): "not_user_authored",
    ("agent_runs", "credential_hash"): "not_user_authored",
    ("agent_runs", "challenge_verified_at"): "not_user_authored",
    ("agent_runs", "harness_state"): "not_user_authored",
    ("agent_runs", "settlement"): "not_user_authored",
    ("agent_runs", "resumed_from_run_id"): "not_user_authored",
    ("agent_runs", "lease_expires_at"): "not_user_authored",
    ("agent_runs", "created_at"): "not_user_authored",
    ("agent_runs", "settled_at"): "not_user_authored",
    ("agent_task_chain", "task_id"): "not_user_authored",
    ("agent_task_chain", "agent_id"): "not_user_authored",
    ("agent_task_messages", "message_id"): "not_user_authored",
    ("agent_task_messages", "task_id"): "not_user_authored",
    ("agent_task_messages", "role"): "not_user_authored",
    ("agent_task_messages", "parts"): "redacted",
    ("agent_task_messages", "created_at"): "not_user_authored",
    ("agent_tasks", "task_id"): "not_user_authored",
    ("agent_tasks", "context_id"): "not_user_authored",
    ("agent_tasks", "room_id"): "not_user_authored",
    ("agent_tasks", "target_agent_id"): "not_user_authored",
    ("agent_tasks", "delegating_agent_id"): "not_user_authored",
    ("agent_tasks", "delegating_run_id"): "not_user_authored",
    ("agent_tasks", "execution_id"): "not_user_authored",
    ("agent_tasks", "state"): "not_user_authored",
    ("agent_tasks", "accepted_output_modes"): "not_user_authored",
    ("agent_tasks", "authorized_by"): "not_user_authored",
    ("agent_tasks", "created_at"): "not_user_authored",
    ("agent_tasks", "updated_at"): "not_user_authored",
    ("agent_tasks", "terminal_at"): "not_user_authored",
    ("agent_tasks", "refusal_reason"): "not_user_authored",
    ("agent_tasks", "requested_by"): "not_user_authored",
    ("agent_templates", "template_id"): "not_user_authored",
    ("agent_templates", "name"): "kept_by_ruling",
    ("agent_templates", "description"): "kept_by_ruling",
    ("agent_templates", "role"): "not_user_authored",
    ("agent_templates", "system_prompt"): "kept_by_ruling",
    ("agent_templates", "capabilities"): "not_user_authored",
    ("agent_templates", "preferred_tools"): "not_user_authored",
    ("agent_templates", "avatar_url"): "not_user_authored",
    ("agent_templates", "created_at"): "not_user_authored",
    ("agent_templates", "workspace_id"): "not_user_authored",
    ("agent_templates", "created_by"): "not_user_authored",
    ("agent_templates", "deleted_at"): "not_user_authored",
    ("agent_templates", "shared_at"): "not_user_authored",
    ("approvals", "approval_id"): "not_user_authored",
    ("approvals", "room_id"): "not_user_authored",
    ("approvals", "execution_id"): "not_user_authored",
    ("approvals", "agent_id"): "not_user_authored",
    ("approvals", "action_description"): "not_user_authored",
    ("approvals", "status"): "not_user_authored",
    ("approvals", "reviewer_id"): "not_user_authored",
    ("approvals", "review_comment"): "redacted",
    ("approvals", "requested_at"): "not_user_authored",
    ("approvals", "reviewed_at"): "not_user_authored",
    ("approvals", "authorized_by"): "not_user_authored",
    ("artifact_claim_sources", "claim_id"): "not_user_authored",
    ("artifact_claim_sources", "output_id"): "not_user_authored",
    ("artifact_claim_sources", "evidence"): "kept_by_ruling",
    ("artifact_claim_sources", "agent_id"): "not_user_authored",
    ("artifact_claim_sources", "execution_id"): "not_user_authored",
    ("artifact_claim_sources", "source_prompt"): "kept_by_ruling",
    ("artifact_claim_sources", "provider_input"): "kept_by_ruling",
    ("artifact_claim_sources", "provider_name"): "not_user_authored",
    ("artifact_claim_sources", "provider_model"): "not_user_authored",
    ("artifact_claim_sources", "provider_response_id"): "not_user_authored",
    ("artifact_claim_sources", "provider_interventions"): "kept_by_ruling",
    ("artifact_claim_sources", "provider_evidence"): "kept_by_ruling",
    ("artifact_claims", "claim_id"): "not_user_authored",
    ("artifact_claims", "version_id"): "not_user_authored",
    ("artifact_claims", "text"): "kept_by_ruling",
    ("artifact_shares", "share_id"): "not_user_authored",
    ("artifact_shares", "artifact_id"): "not_user_authored",
    ("artifact_shares", "room_id"): "not_user_authored",
    ("artifact_shares", "token_hash"): "not_user_authored",
    ("artifact_shares", "created_by"): "not_user_authored",
    ("artifact_shares", "created_at"): "not_user_authored",
    ("artifact_shares", "revoked_at"): "not_user_authored",
    ("artifact_versions", "version_id"): "not_user_authored",
    ("artifact_versions", "artifact_id"): "not_user_authored",
    ("artifact_versions", "content"): "kept_by_ruling",
    ("artifact_versions", "content_hash"): "not_user_authored",
    ("artifact_versions", "created_by"): "not_user_authored",
    ("artifact_versions", "created_at"): "not_user_authored",
    ("artifact_versions", "provenance_hash"): "not_user_authored",
    ("artifact_versions", "branch_synthesis_id"): "not_user_authored",
    ("artifacts", "artifact_id"): "not_user_authored",
    ("artifacts", "room_id"): "not_user_authored",
    ("artifacts", "name"): "redacted",
    ("artifacts", "artifact_type"): "not_user_authored",
    ("artifacts", "description"): "redacted",
    ("artifacts", "created_by"): "not_user_authored",
    ("artifacts", "created_at"): "not_user_authored",
    ("artifacts", "updated_at"): "not_user_authored",
    ("attachments", "attachment_id"): "not_user_authored",
    ("attachments", "room_id"): "not_user_authored",
    ("attachments", "uploader_id"): "not_user_authored",
    ("attachments", "filename"): "redacted",
    ("attachments", "content_type"): "not_user_authored",
    ("attachments", "sha256"): "not_user_authored",
    ("attachments", "created_at"): "not_user_authored",
    ("attachments", "message_id"): "not_user_authored",
    ("branch_syntheses", "synthesis_id"): "not_user_authored",
    ("branch_syntheses", "branch_id"): "not_user_authored",
    ("branch_syntheses", "room_id"): "not_user_authored",
    ("branch_syntheses", "synthesis_type"): "not_user_authored",
    ("branch_syntheses", "status"): "not_user_authored",
    ("branch_syntheses", "title"): "redacted",
    ("branch_syntheses", "initiated_by"): "not_user_authored",
    ("branch_syntheses", "provider_input"): "not_user_authored",
    ("branch_syntheses", "provider_name"): "not_user_authored",
    ("branch_syntheses", "provider_model"): "not_user_authored",
    ("branch_syntheses", "provider_response_id"): "not_user_authored",
    ("branch_syntheses", "provider_evidence"): "not_user_authored",
    ("branch_syntheses", "content"): "not_user_authored",
    ("branch_syntheses", "error"): "not_user_authored",
    ("branch_syntheses", "artifact_version_id"): "not_user_authored",
    ("branch_syntheses", "created_at"): "not_user_authored",
    ("branch_syntheses", "completed_at"): "not_user_authored",
    ("branch_synthesis_inputs", "synthesis_id"): "not_user_authored",
    ("branch_synthesis_inputs", "output_id"): "not_user_authored",
    ("branches", "branch_id"): "not_user_authored",
    ("branches", "room_id"): "not_user_authored",
    ("branches", "mode"): "not_user_authored",
    ("branches", "status"): "not_user_authored",
    ("branches", "initiated_by"): "not_user_authored",
    ("branches", "initiating_prompt"): "redacted",
    ("branches", "context_message_ids"): "not_user_authored",
    ("branches", "context_snapshot"): "not_user_authored",
    ("branches", "context_hash"): "not_user_authored",
    ("branches", "created_at"): "not_user_authored",
    ("branches", "updated_at"): "not_user_authored",
    ("branches", "completed_at"): "not_user_authored",
    ("credentials", "credential_id"): "not_user_authored",
    ("credentials", "org_id"): "not_user_authored",
    ("credentials", "name"): "kept_by_ruling",
    ("credentials", "credential_type"): "not_user_authored",
    ("credentials", "encrypted_data"): "not_user_authored",
    ("credentials", "created_by"): "not_user_authored",
    ("credentials", "created_at"): "not_user_authored",
    ("decisions", "decision_id"): "not_user_authored",
    ("decisions", "room_id"): "not_user_authored",
    ("decisions", "title"): "redacted",
    ("decisions", "content"): "redacted",
    ("decisions", "reason"): "redacted",
    ("decisions", "status"): "not_user_authored",
    ("decisions", "created_by"): "not_user_authored",
    ("decisions", "reviewed_by"): "not_user_authored",
    ("decisions", "created_at"): "not_user_authored",
    ("event_redactions", "redaction_id"): "not_user_authored",
    ("event_redactions", "event_id"): "not_user_authored",
    ("event_redactions", "room_id"): "not_user_authored",
    ("event_redactions", "original_event_hash"): "not_user_authored",
    ("event_redactions", "redacted_at"): "not_user_authored",
    ("event_redactions", "reason"): "not_user_authored",
    ("event_redactions", "actor_id"): "not_user_authored",
    ("execution_callers", "execution_id"): "not_user_authored",
    ("execution_callers", "caller_id"): "not_user_authored",
    ("execution_callers", "first_acted_at"): "not_user_authored",
    ("execution_interventions", "intervention_id"): "not_user_authored",
    ("execution_interventions", "execution_id"): "not_user_authored",
    ("execution_interventions", "intervened_by"): "not_user_authored",
    ("execution_interventions", "instruction"): "redacted",
    ("execution_interventions", "created_at"): "not_user_authored",
    ("execution_interventions", "consumed_at"): "not_user_authored",
    ("executions", "execution_id"): "not_user_authored",
    ("executions", "session_id"): "not_user_authored",
    ("executions", "agent_id"): "not_user_authored",
    ("executions", "run_id"): "not_user_authored",
    ("executions", "status"): "not_user_authored",
    ("executions", "input_data"): "redacted",
    ("executions", "output_data"): "not_user_authored",
    ("executions", "error"): "not_user_authored",
    ("executions", "started_at"): "not_user_authored",
    ("executions", "completed_at"): "not_user_authored",
    ("executions", "branch_id"): "not_user_authored",
    ("executions", "triggered_by"): "not_user_authored",
    ("executions", "authorized_by"): "not_user_authored",
    ("executions", "dispatch_claim"): "not_user_authored",
    ("executions", "agent_task_id"): "not_user_authored",
    ("idempotency_keys", "scope_id"): "not_user_authored",
    ("idempotency_keys", "user_id"): "not_user_authored",
    ("idempotency_keys", "idempotency_key"): "not_user_authored",
    ("idempotency_keys", "operation"): "not_user_authored",
    ("idempotency_keys", "request_hash"): "not_user_authored",
    ("idempotency_keys", "result_ref"): "not_user_authored",
    ("idempotency_keys", "created_at"): "not_user_authored",
    ("memories", "memory_id"): "not_user_authored",
    ("memories", "room_id"): "not_user_authored",
    ("memories", "workspace_id"): "not_user_authored",
    ("memories", "org_id"): "not_user_authored",
    ("memories", "scope"): "not_user_authored",
    ("memories", "content"): "redacted",
    ("memories", "memory_type"): "not_user_authored",
    ("memories", "superseded_by"): "not_user_authored",
    ("memories", "created_by"): "not_user_authored",
    ("memories", "created_at"): "not_user_authored",
    ("message_mentions", "message_id"): "not_user_authored",
    ("message_mentions", "room_id"): "not_user_authored",
    ("message_mentions", "target_type"): "not_user_authored",
    ("message_mentions", "target_id"): "not_user_authored",
    ("message_mentions", "handle"): "not_user_authored",
    ("message_mentions", "invoked_execution_id"): "not_user_authored",
    ("message_mentions", "created_at"): "not_user_authored",
    ("message_reactions", "message_id"): "not_user_authored",
    ("message_reactions", "room_id"): "not_user_authored",
    ("message_reactions", "actor_id"): "not_user_authored",
    ("message_reactions", "emoji"): "not_user_authored",
    ("message_reactions", "created_at"): "not_user_authored",
    ("message_reactions", "updated_at"): "not_user_authored",
    ("message_reactions", "removed_at"): "not_user_authored",
    ("message_reactions", "actor_type"): "not_user_authored",
    ("messages", "message_id"): "not_user_authored",
    ("messages", "room_id"): "not_user_authored",
    ("messages", "role"): "not_user_authored",
    ("messages", "sender_id"): "not_user_authored",
    ("messages", "content"): "redacted",
    ("messages", "metadata"): "not_user_authored",
    ("messages", "created_at"): "not_user_authored",
    ("messages", "parent_message_id"): "not_user_authored",
    ("messages", "root_message_id"): "not_user_authored",
    ("notifications", "notification_id"): "not_user_authored",
    ("notifications", "user_id"): "not_user_authored",
    ("notifications", "room_id"): "not_user_authored",
    ("notifications", "title"): "kept_by_ruling",
    ("notifications", "body"): "kept_by_ruling",
    ("notifications", "notification_type"): "not_user_authored",
    ("notifications", "status"): "not_user_authored",
    ("notifications", "created_at"): "not_user_authored",
    ("oidc_authorizations", "state"): "not_user_authored",
    ("oidc_authorizations", "nonce"): "not_user_authored",
    ("oidc_authorizations", "code_verifier"): "not_user_authored",
    ("oidc_authorizations", "browser_binding_hash"): "not_user_authored",
    ("oidc_authorizations", "created_at"): "not_user_authored",
    ("oidc_authorizations", "expires_at"): "not_user_authored",
    ("oidc_authorizations", "consumed_at"): "not_user_authored",
    ("oidc_logout_tokens", "jti"): "not_user_authored",
    ("oidc_logout_tokens", "issuer"): "not_user_authored",
    ("oidc_logout_tokens", "seen_at"): "not_user_authored",
    ("ontology_entities", "entity_id"): "not_user_authored",
    ("ontology_entities", "room_id"): "not_user_authored",
    ("ontology_entities", "kind"): "not_user_authored",
    ("ontology_entities", "source_object_id"): "not_user_authored",
    ("ontology_entities", "label"): "not_user_authored",
    ("ontology_entities", "properties"): "not_user_authored",
    ("ontology_entities", "derivation_kind"): "not_user_authored",
    ("ontology_entities", "evidence_ids"): "not_user_authored",
    ("ontology_entities", "source_ids"): "not_user_authored",
    ("ontology_entities", "review_status"): "not_user_authored",
    ("ontology_entities", "created_at"): "not_user_authored",
    ("ontology_entities", "updated_at"): "not_user_authored",
    ("ontology_entities", "extractor"): "not_user_authored",
    ("ontology_entities", "evidence_event_sequences"): "not_user_authored",
    ("ontology_extraction_cursors", "room_id"): "not_user_authored",
    ("ontology_extraction_cursors", "extractor"): "not_user_authored",
    ("ontology_extraction_cursors", "last_run_at"): "not_user_authored",
    ("ontology_relationships", "relationship_id"): "not_user_authored",
    ("ontology_relationships", "room_id"): "not_user_authored",
    ("ontology_relationships", "kind"): "not_user_authored",
    ("ontology_relationships", "from_entity_id"): "not_user_authored",
    ("ontology_relationships", "to_entity_id"): "not_user_authored",
    ("ontology_relationships", "derivation_kind"): "not_user_authored",
    ("ontology_relationships", "evidence_ids"): "not_user_authored",
    ("ontology_relationships", "source_ids"): "not_user_authored",
    ("ontology_relationships", "review_status"): "not_user_authored",
    ("ontology_relationships", "created_at"): "not_user_authored",
    ("ontology_relationships", "updated_at"): "not_user_authored",
    ("ontology_relationships", "extractor"): "not_user_authored",
    ("ontology_relationships", "evidence_event_sequences"): "not_user_authored",
    ("ontology_relationships", "source_object_kind"): "not_user_authored",
    ("ontology_relationships", "source_object_id"): "not_user_authored",
    ("ontology_reviews", "review_id"): "not_user_authored",
    ("ontology_reviews", "room_id"): "not_user_authored",
    ("ontology_reviews", "target_type"): "not_user_authored",
    ("ontology_reviews", "target_id"): "not_user_authored",
    ("ontology_reviews", "action"): "not_user_authored",
    ("ontology_reviews", "before_value"): "kept_by_ruling",
    ("ontology_reviews", "after_value"): "kept_by_ruling",
    ("ontology_reviews", "reason"): "kept_by_ruling",
    ("ontology_reviews", "reviewed_by"): "not_user_authored",
    ("ontology_reviews", "created_at"): "not_user_authored",
    ("organization_members", "org_id"): "not_user_authored",
    ("organization_members", "user_id"): "not_user_authored",
    ("organization_members", "role"): "not_user_authored",
    ("organization_members", "created_at"): "not_user_authored",
    ("organizations", "org_id"): "not_user_authored",
    ("organizations", "name"): "kept_by_ruling",
    ("organizations", "slug"): "kept_by_ruling",
    ("organizations", "created_at"): "not_user_authored",
    ("output_selections", "room_id"): "not_user_authored",
    ("output_selections", "output_id"): "not_user_authored",
    ("output_selections", "disposition"): "not_user_authored",
    ("output_selections", "decided_by"): "not_user_authored",
    ("output_selections", "updated_at"): "not_user_authored",
    ("output_selections", "branch_id"): "not_user_authored",
    ("room_events", "event_id"): "not_user_authored",
    ("room_events", "room_id"): "not_user_authored",
    ("room_events", "event_type"): "not_user_authored",
    ("room_events", "payload"): "redacted",
    ("room_events", "actor_id"): "not_user_authored",
    ("room_events", "actor_type"): "not_user_authored",
    ("room_events", "timestamp"): "not_user_authored",
    ("room_events", "prev_hash"): "not_user_authored",
    ("room_events", "event_hash"): "not_user_authored",
    ("room_members", "room_id"): "not_user_authored",
    ("room_members", "user_id"): "not_user_authored",
    ("room_members", "role"): "not_user_authored",
    ("room_members", "joined_at"): "not_user_authored",
    ("room_members", "allowed_capabilities"): "not_user_authored",
    ("room_participant_handles", "room_id"): "not_user_authored",
    ("room_participant_handles", "participant_type"): "not_user_authored",
    ("room_participant_handles", "participant_id"): "not_user_authored",
    ("room_participant_handles", "handle"): "redacted",
    ("room_participant_handles", "created_at"): "not_user_authored",
    ("room_postures", "declaration_id"): "not_user_authored",
    ("room_postures", "room_id"): "not_user_authored",
    ("room_postures", "posture"): "not_user_authored",
    ("room_postures", "declared_by"): "not_user_authored",
    ("room_postures", "declared_at"): "not_user_authored",
    ("room_read_cursors", "room_id"): "not_user_authored",
    ("room_read_cursors", "user_id"): "not_user_authored",
    ("room_read_cursors", "updated_at"): "not_user_authored",
    ("room_sequences", "room_id"): "not_user_authored",
    ("room_templates", "template_id"): "not_user_authored",
    ("room_templates", "workspace_id"): "not_user_authored",
    ("room_templates", "name"): "kept_by_ruling",
    ("room_templates", "description"): "kept_by_ruling",
    ("room_templates", "agent_template_ids"): "not_user_authored",
    ("room_templates", "created_by"): "not_user_authored",
    ("room_templates", "created_at"): "not_user_authored",
    ("room_templates", "deleted_at"): "not_user_authored",
    ("rooms", "room_id"): "not_user_authored",
    ("rooms", "workspace_id"): "not_user_authored",
    ("rooms", "name"): "redacted",
    ("rooms", "description"): "redacted",
    ("rooms", "status"): "not_user_authored",
    ("rooms", "created_by"): "not_user_authored",
    ("rooms", "created_at"): "not_user_authored",
    ("rooms", "allowed_capabilities"): "not_user_authored",
    ("search_documents", "object_kind"): "not_user_authored",
    ("search_documents", "object_id"): "not_user_authored",
    ("search_documents", "room_id"): "not_user_authored",
    ("search_documents", "author_id"): "not_user_authored",
    ("search_documents", "content"): "redacted",
    ("search_documents", "created_at"): "not_user_authored",
    ("search_documents", "container_id"): "not_user_authored",
    ("search_indexed_kinds", "object_kind"): "not_user_authored",
    ("search_indexed_kinds", "indexed_at"): "not_user_authored",
    ("session_refresh_tokens", "token_hash"): "not_user_authored",
    ("session_refresh_tokens", "session_id"): "not_user_authored",
    ("session_refresh_tokens", "issued_at"): "not_user_authored",
    ("session_refresh_tokens", "expires_at"): "not_user_authored",
    ("session_refresh_tokens", "consumed_at"): "not_user_authored",
    ("session_refresh_tokens", "replaced_by_hash"): "not_user_authored",
    ("sessions", "session_id"): "not_user_authored",
    ("sessions", "room_id"): "not_user_authored",
    ("sessions", "agent_id"): "not_user_authored",
    ("sessions", "task_id"): "not_user_authored",
    ("sessions", "status"): "not_user_authored",
    ("sessions", "started_at"): "not_user_authored",
    ("sessions", "ended_at"): "not_user_authored",
    ("suspended_turns", "execution_id"): "not_user_authored",
    ("suspended_turns", "prompt"): "redacted",
    ("suspended_turns", "acting_as"): "not_user_authored",
    ("suspended_turns", "observations"): "not_user_authored",
    ("suspended_turns", "suspended_at"): "not_user_authored",
    ("task_dependencies", "task_id"): "not_user_authored",
    ("task_dependencies", "depends_on_task_id"): "not_user_authored",
    ("task_dependencies", "created_at"): "not_user_authored",
    ("tasks", "task_id"): "not_user_authored",
    ("tasks", "room_id"): "not_user_authored",
    ("tasks", "title"): "redacted",
    ("tasks", "description"): "redacted",
    ("tasks", "status"): "not_user_authored",
    ("tasks", "priority"): "not_user_authored",
    ("tasks", "assigned_agent_id"): "not_user_authored",
    ("tasks", "created_by"): "not_user_authored",
    ("tasks", "parent_task_id"): "not_user_authored",
    ("tasks", "delegation_id"): "not_user_authored",
    ("tasks", "created_at"): "not_user_authored",
    ("tasks", "updated_at"): "not_user_authored",
    ("tool_permissions", "permission_id"): "not_user_authored",
    ("tool_permissions", "agent_id"): "not_user_authored",
    ("tool_permissions", "room_id"): "not_user_authored",
    ("tool_permissions", "tool_name"): "not_user_authored",
    ("tool_permissions", "created_at"): "not_user_authored",
    ("tool_request_reviewers", "request_id"): "not_user_authored",
    ("tool_request_reviewers", "reviewer_id"): "not_user_authored",
    ("tool_request_reviewers", "reviewed_at"): "not_user_authored",
    ("tool_requests", "request_id"): "not_user_authored",
    ("tool_requests", "room_id"): "not_user_authored",
    ("tool_requests", "execution_id"): "not_user_authored",
    ("tool_requests", "agent_id"): "not_user_authored",
    ("tool_requests", "requested_by"): "not_user_authored",
    ("tool_requests", "tool"): "not_user_authored",
    ("tool_requests", "input_json"): "not_user_authored",
    ("tool_requests", "required_capability"): "not_user_authored",
    ("tool_requests", "effective_json"): "not_user_authored",
    ("tool_requests", "status"): "not_user_authored",
    ("tool_requests", "reason"): "not_user_authored",
    ("tool_requests", "approval_id"): "not_user_authored",
    ("tool_requests", "result_json"): "not_user_authored",
    ("tool_requests", "created_at"): "not_user_authored",
    ("tool_requests", "resolved_at"): "not_user_authored",
    ("tool_requests", "authorized_by"): "not_user_authored",
    ("turn_locks", "lock_id"): "not_user_authored",
    ("turn_locks", "scope_type"): "not_user_authored",
    ("turn_locks", "scope_id"): "not_user_authored",
    ("turn_locks", "branch_id"): "not_user_authored",
    ("turn_locks", "status"): "not_user_authored",
    ("turn_locks", "acquired_by"): "not_user_authored",
    ("turn_locks", "acquired_at"): "not_user_authored",
    ("turn_locks", "released_at"): "not_user_authored",
    ("turn_locks", "release_reason"): "not_user_authored",
    ("user_bootstrap_contexts", "user_id"): "not_user_authored",
    ("user_bootstrap_contexts", "org_id"): "not_user_authored",
    ("user_bootstrap_contexts", "workspace_id"): "not_user_authored",
    ("user_bootstrap_contexts", "room_id"): "not_user_authored",
    ("user_bootstrap_contexts", "created_at"): "not_user_authored",
    ("user_sessions", "session_id"): "not_user_authored",
    ("user_sessions", "user_id"): "not_user_authored",
    ("user_sessions", "issuer"): "not_user_authored",
    ("user_sessions", "subject"): "not_user_authored",
    ("user_sessions", "idp_session_id"): "not_user_authored",
    ("user_sessions", "created_at"): "not_user_authored",
    ("user_sessions", "idle_expires_at"): "not_user_authored",
    ("user_sessions", "absolute_expires_at"): "not_user_authored",
    ("user_sessions", "revoked_at"): "not_user_authored",
    ("user_sessions", "revoked_reason"): "not_user_authored",
    ("user_sessions", "idp_id_token"): "not_user_authored",
    ("user_sessions", "idp_refresh_token"): "not_user_authored",
    ("user_tokens", "token_hash"): "not_user_authored",
    ("user_tokens", "user_id"): "not_user_authored",
    ("user_tokens", "label"): "not_user_authored",
    ("user_tokens", "created_at"): "not_user_authored",
    ("user_tokens", "revoked_at"): "not_user_authored",
    ("user_tokens", "session_id"): "not_user_authored",
    ("user_tokens", "expires_at"): "not_user_authored",
    ("users", "user_id"): "not_user_authored",
    ("users", "display_name"): "redacted",
    ("users", "email"): "redacted",
    ("users", "avatar_url"): "redacted",
    ("users", "status"): "not_user_authored",
    ("users", "created_at"): "not_user_authored",
    ("workspace_members", "workspace_id"): "not_user_authored",
    ("workspace_members", "user_id"): "not_user_authored",
    ("workspace_members", "role"): "not_user_authored",
    ("workspace_members", "created_at"): "not_user_authored",
    ("workspaces", "workspace_id"): "not_user_authored",
    ("workspaces", "org_id"): "not_user_authored",
    ("workspaces", "name"): "kept_by_ruling",
    ("workspaces", "slug"): "kept_by_ruling",
    ("workspaces", "created_at"): "not_user_authored",
    ("workspaces", "allowed_capabilities"): "not_user_authored",
}


def _is_redaction_marker(payload: dict[str, Any]) -> bool:
    return payload.get("redacted") is True and isinstance(payload.get("redaction_id"), str)


def _carries_personal_content(event_type: str, payload: dict[str, Any]) -> bool:
    if any(isinstance(payload.get(key), str) and payload[key] for key in _PERSONAL_PAYLOAD_KEYS):
        return True
    if event_type == EventType.ROOM_CREATED.value:
        return any(
            isinstance(payload.get(key), str) and payload[key] for key in _ROOM_METADATA_KEYS
        )
    if event_type == EventType.ARTIFACT_CREATED.value:
        # A hand-created artifact's payload carries its name verbatim. A
        # synthesis-published one reuses the same event type and the same key,
        # but the name is always one of the fixed spec names
        # (RESERVED_ARTIFACT_NAMES), never anything the erased user typed.
        name = payload.get("name")
        return isinstance(name, str) and bool(name) and name not in RESERVED_ARTIFACT_NAMES
    if event_type == EventType.TASK_DELEGATED.value:
        # This one event type carries every move of a task's whole lifecycle
        # (agent_tasks.py::_append_agent_task_event), never the asker's typed
        # words itself -- those live in agent_task_messages.parts, a durable
        # table this event's payload only ever points at by task_id. What the
        # payload does carry is delegating_agent_id, and it is None on exactly
        # the moves a human opened or continued directly: a delegated
        # sub-task's own events are never reached here at all, since their
        # actor_type is "agent" and list_by_actor already filtered those out.
        # So a human-opened task's every lifecycle event is treated as
        # carrying personal content, and the redaction hangs on whichever of
        # them this row is, per the ruling: no new event is invented for this.
        return payload.get("delegating_agent_id") is None
    return False


class _ErasureMixin(_SharedMixin):
    """Mixin providing user erasure: tombstoning, redaction, and attachment removal."""

    async def erase_user(self, user_id: str) -> dict[str, Any]:
        """Tombstone the user and redact every personal event they authored.

        Idempotent: an event already carrying a marker payload is left alone, a
        tombstone written again writes the same values, and a session already
        revoked stays untouched by the same guarded UPDATE. A second call against
        an already erased user therefore reports zero new redactions rather than
        failing or duplicating anything.
        """
        user = await self.repos.users.get(user_id)
        if user is None:
            raise DomainError(f"user not found: {user_id}")

        # "user" is how most of the log spells a human actor; a message they sent
        # is stamped with its role lowercased instead, which is "human". Both name
        # this same person, never an agent (agent events are always "agent").
        by_room: dict[str, list[dict[str, Any]]] = {}
        for row in await self.repos.events.list_by_actor(user_id, ("user", "human")):
            by_room.setdefault(str(row["room_id"]), []).append(row)

        rooms_touched: list[str] = []
        total_redactions = 0
        # Dedup across a task's several TASK_DELEGATED events (open, continue,
        # ...): the durable agent_task_messages sweep below is keyed by
        # task_id, not by event, so it only needs to run once per task even
        # when several of this user's own events name the same one.
        agent_task_ids_swept: set[str] = set()
        for room_id, rows in by_room.items():
            redaction_ids: list[str] = []
            async with self.db.transaction():
                for row in rows:
                    payload = json.loads(str(row["payload"]))
                    event_type = str(row["event_type"])
                    if _is_redaction_marker(payload) or not _carries_personal_content(
                        event_type, payload
                    ):
                        continue
                    event_id = str(row["event_id"])
                    redaction_id = new_id("redact")
                    await self.repos.event_redactions.create_in_transaction(
                        redaction_id,
                        event_id,
                        room_id,
                        str(row["event_hash"]),
                        utcnow(),
                        _REDACTION_REASON,
                        ERASURE_OPERATOR_ID,
                    )
                    marker = json.dumps({"redacted": True, "redaction_id": redaction_id})
                    await self.repos.events.redact_payload_in_transaction(event_id, marker)
                    message_id = payload.get("message_id")
                    if isinstance(message_id, str) and message_id:
                        await self.repos.messages.redact_content_in_transaction(message_id, marker)
                        # The search index is its own copy of the message text, made
                        # at send time: redacting the message row above leaves this
                        # copy untouched unless it is dropped here too.
                        await self.repos.search.forget_in_transaction(
                            SearchObjectKind.MESSAGE, message_id
                        )
                    task_id = payload.get("task_id")
                    if event_type == EventType.TASK_CREATED.value and isinstance(task_id, str):
                        # tasks.title/description is its own copy, read by every task
                        # listing directly, and search_documents holds a third copy
                        # of the title made at create/update time.
                        await self.repos.tasks.redact_content_in_transaction(task_id, marker)
                        await self.repos.search.forget_in_transaction(
                            SearchObjectKind.TASK, task_id
                        )
                    decision_id = payload.get("decision_id")
                    if event_type == EventType.DECISION_CREATED.value and isinstance(
                        decision_id, str
                    ):
                        await self.repos.decisions.redact_content_in_transaction(
                            decision_id, marker
                        )
                        await self.repos.search.forget_in_transaction(
                            SearchObjectKind.DECISION, decision_id
                        )
                    if event_type == EventType.TASK_DELEGATED.value and isinstance(task_id, str):
                        # No SearchObjectKind exists for an agent task's own
                        # messages (checked domain/models.py::SearchObjectKind):
                        # they are never indexed, so there is no search copy to
                        # sweep, only the durable parts column itself.
                        if task_id not in agent_task_ids_swept:
                            await self.repos.agent_tasks.redact_asker_parts_in_transaction(
                                task_id, marker
                            )
                            agent_task_ids_swept.add(task_id)
                    if event_type == EventType.ROOM_CREATED.value:
                        # rooms has no search_documents kind of its own to sweep.
                        await self.repos.rooms.redact_metadata_in_transaction(room_id, marker)
                    artifact_id = payload.get("artifact_id")
                    if event_type == EventType.ARTIFACT_CREATED.value and isinstance(
                        artifact_id, str
                    ):
                        # Only the name/description live here; the version content
                        # this may have been created with is append-only evidence
                        # (migration 003) and is out of reach of this redaction.
                        await self.repos.artifacts.redact_metadata_in_transaction(
                            artifact_id, marker
                        )
                    # A branch context snapshot is also its own copy, taken whenever a
                    # branch started while this row was still live. Same reasoning:
                    # the copy survives the redaction above unless swept separately.
                    await self.repos.branches.redact_message_in_context_snapshots_in_transaction(
                        room_id,
                        message_id if isinstance(message_id, str) and message_id else None,
                        event_id,
                        marker,
                    )
                    redaction_ids.append(redaction_id)
                if redaction_ids:
                    await self.repos.events.append_with_next_sequence_in_transaction(
                        RoomEvent(
                            room_id=room_id,
                            sequence=0,
                            event_type=EventType.EVENT_REDACTED,
                            payload={
                                "redaction_ids": redaction_ids,
                                "count": len(redaction_ids),
                            },
                            actor_id=ERASURE_OPERATOR_ID,
                            actor_type="system",
                        )
                    )
                # Round 6: every intervention this user steered in this room
                # gets its durable instruction column scrubbed here too, the
                # same transaction the human_redirected_agent event carrying
                # the same text was just redacted in above. A room only ever
                # reaches this loop because some row of this user's own was
                # found in it, so a room she only ever intervened in (with no
                # other authored event) is never skipped: her intervene call
                # itself appended one such event.
                intervention_ids = await self.repos.interventions.list_ids_by_intervener_in_room(
                    room_id, user_id
                )
                for intervention_id in intervention_ids:
                    await self.repos.interventions.redact_instruction_in_transaction(
                        intervention_id, _NO_CHAIN_MARKER
                    )
            if redaction_ids:
                rooms_touched.append(room_id)
                total_redactions += len(redaction_ids)

        # Every attachment this user uploaded, whether or not the message that
        # claimed it survived redaction above, and whether or not it was ever
        # claimed at all.
        for attachment_id in await self.repos.attachments.list_ids_by_uploader(user_id):
            async with self.db.transaction():
                await self.repos.attachments.erase_in_transaction(attachment_id)

        # A branch synthesis title is user-typed but never rides inside any
        # chained event payload (branch.synthesis.started carries only ids), so
        # it cannot be caught by the per-room loop above; it is swept the same
        # way attachments are, by its own author column.
        for synthesis_id in await self.repos.branch_syntheses.list_ids_by_initiator(user_id):
            async with self.db.transaction():
                await self.repos.branch_syntheses.redact_title_in_transaction(
                    synthesis_id, _NO_CHAIN_MARKER
                )

        # A branch's initiating_prompt is user-typed but, like a synthesis
        # title, never rides inside any chained event payload (branch.started
        # carries only branch_id, mode, status, and context bookkeeping): it
        # cannot be caught by the per-room loop above either. Swept the same
        # way, by its own author column, plus the independent copy every
        # execution the branch launched keeps in its own input_data.
        for branch_id in await self.repos.branches.list_ids_by_initiator(user_id):
            async with self.db.transaction():
                await self.repos.branches.redact_initiating_prompt_in_transaction(
                    branch_id, _NO_CHAIN_MARKER
                )
                await self.repos.executions.redact_initiating_prompt_in_transaction(
                    branch_id, _NO_CHAIN_MARKER
                )

        # A memory's content is user-typed but can belong to a workspace or an
        # org with no room at all, and even when it does carry a room_id its
        # ``memory.created`` payload never carries the content itself: swept the
        # same way, by its own author column.
        for memory_id in await self.repos.memories.list_ids_by_creator(user_id):
            async with self.db.transaction():
                await self.repos.memories.redact_content_in_transaction(memory_id, _NO_CHAIN_MARKER)

        # A reviewer's approval comment is user-typed but never rides inside
        # ``approval.granted``/``approval.rejected``'s payload either (both carry
        # only the approval and reviewer ids): swept the same way, by its own
        # author column, keyed by the reviewer rather than the room actor a
        # branch or a message is keyed by.
        for approval_id in await self.repos.approvals.list_ids_by_reviewer(user_id):
            async with self.db.transaction():
                await self.repos.approvals.redact_review_comment_in_transaction(
                    approval_id, _NO_CHAIN_MARKER
                )

        # A suspended turn parks a run mid-tool-call behind a reviewer, holding
        # the erased human's own steer text (suspended_turns.prompt) durably
        # until that reviewer decides. If nobody ever does, the row would
        # otherwise wait forever on a person who no longer exists. Rather than
        # blank the text column in place, the run it belongs to is settled
        # AUTHORITY_REVOKED, the same settlement this codebase already uses
        # elsewhere for a run whose bounding principal's authority no longer
        # holds (services/_shared.py, services/runs.py, services/steps.py):
        # settling discards the suspended_turns row as one of its own steps
        # (``_settle_run``), so the pending prompt is gone because the row
        # holding it is gone, not because a column in it was overwritten.
        for execution_id in await self.repos.suspended_turns.list_execution_ids_by_acting_as(
            user_id
        ):
            run = await self.repos.agent_runs.get_by_execution(execution_id)
            if run is None:
                # No run row left to settle against; nothing to do but drop
                # the parked text directly.
                await self.repos.suspended_turns.discard(execution_id)
                continue
            await self._settle_run(
                run, RunSettlement.AUTHORITY_REVOKED, ERASURE_OPERATOR_ID, _ABANDONED_TURN_REASON
            )

        # An execution intervention's own instruction column was scrubbed room by
        # room above; a run whose only *unconsumed* steer was this user's has
        # nobody left who could ever narrow it further, the same shape a
        # suspended turn's abandoned reviewer is. Unlike a suspended turn, the
        # intervention row itself is not discarded (it stays as the audit record
        # of who steered and when, per migrations 018/020) -- only the run
        # waiting on it is brought to a terminal state instead of left open.
        for execution_id in await self.repos.interventions.list_execution_ids_with_only_own_pending(
            user_id
        ):
            run = await self.repos.agent_runs.get_by_execution(execution_id)
            if run is None:
                continue
            await self._settle_run(
                run,
                RunSettlement.AUTHORITY_REVOKED,
                ERASURE_OPERATOR_ID,
                _ABANDONED_INTERVENTION_REASON,
            )

        moment = utcnow()
        async with self.db.transaction():
            await self.repos.users.tombstone_in_transaction(user_id)
            await self.repos.user_sessions.revoke_all_for_user_in_transaction(user_id, moment)
        for handle_room_id in await self.repos.handles.list_rooms_for_participant(
            ParticipantType.USER, user_id
        ):
            await self.repos.handles.release_in_transaction(
                handle_room_id, ParticipantType.USER, user_id
            )

        return {
            "user_id": user_id,
            "rooms_touched": rooms_touched,
            "redactions": total_redactions,
        }
