-- A branch's initiating_prompt is what a human typed to open it. It never
-- rides inside a chained room_events payload: branch.started (007/services/
-- branches.py) carries only branch_id, mode, status, and context bookkeeping
-- (context_event_sequence, context_message_ids, context_hash,
-- execution_ids), never the prompt text itself. That means nothing else in
-- this schema's event log can reach it: the column is the only live copy,
-- and there is no event_redactions row or EVENT_REDACTED naming for it, the
-- same shape as 047's synthesis title.
--
-- 046 already narrowed 007's branches_reject_context_update once, to let
-- context_snapshot change for erasure. initiating_prompt was deliberately
-- left immutable at that point because nothing needed to touch it yet. Now
-- something does: the exception widens to cover this one additional column,
-- and nothing else. room_id, initiated_by, context_event_sequence,
-- context_message_ids, and context_hash stay exactly as immutable as 046
-- left them.

DROP TRIGGER IF EXISTS branches_reject_context_update;

CREATE TRIGGER IF NOT EXISTS branches_reject_context_update
BEFORE UPDATE OF room_id, initiated_by, context_event_sequence,
    context_message_ids, context_hash ON branches
BEGIN
    SELECT RAISE(ABORT, 'branch context boundary is immutable');
END;
