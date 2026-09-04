-- A branch's context_snapshot is its own literal copy of the room's recent
-- messages and events, made once at branch-start time. 007's trigger blocks
-- any update to it after creation, on purpose: agents and auditors need to
-- trust that the frozen input a branch reasoned over never quietly changes
-- underneath them.
--
-- Erasure needs one narrow exception to that rule. A message a person typed
-- can already be inside a snapshot when they are erased, and nothing else
-- in this schema can reach that copy: room_events and messages get a
-- marker, but this table's own copy of the same text was never touched by
-- that redaction, so it would otherwise go on quoting the original words
-- forever. That is not the kind of change 007 was written to block: it is
-- the same content-only replacement room_events already allows (the payload
-- changes, event_hash and prev_hash do not), applied to the one other table
-- that keeps its own copy of message text.
--
-- The exception is narrow on purpose: room_id, initiated_by,
-- initiating_prompt, context_event_sequence, context_message_ids, and
-- context_hash stay exactly as immutable as 007 made them. Only
-- context_snapshot may change, and only erasure's own repository method
-- (BranchRepo.redact_message_in_context_snapshots_in_transaction) ever
-- issues that update.

DROP TRIGGER IF EXISTS branches_reject_context_update;

CREATE TRIGGER IF NOT EXISTS branches_reject_context_update
BEFORE UPDATE OF room_id, initiated_by, initiating_prompt, context_event_sequence,
    context_message_ids, context_hash ON branches
BEGIN
    SELECT RAISE(ABORT, 'branch context boundary is immutable');
END;
