-- 018 made an intervention's ``instruction`` immutable alongside ``intervened_by``,
-- reasoning that a steer is bounded by the authority that produced it and that
-- bound is a row rather than a local variable. 020 already carved the first crack
-- in that: the row cannot even keep the capability set it once froze, because a
-- later step re-derives authority from durable records instead of trusting a
-- stale copy. The instruction text was never the authority anyway — who steered
-- is, and that stays exactly as immutable as 018 made it.
--
-- The erased user's own typed words belong to the same class of content this
-- track already redacts everywhere else it lives (a message, a task title, a
-- branch's initiating prompt): something a person typed, not a fact the system
-- derived. Round 4 filed this column as a deliberate exception because nothing
-- here narrowed the trigger yet; round 6 narrows it, the same shape 047 and 048
-- narrowed theirs, so the durable copy can finally be scrubbed alongside the
-- chained ``human_redirected_agent`` event payload that already carries the same
-- text and is already redacted.

DROP TRIGGER IF EXISTS execution_interventions_reject_authority_update;

CREATE TRIGGER execution_interventions_reject_authority_update
BEFORE UPDATE OF intervened_by ON execution_interventions
BEGIN
    SELECT RAISE(ABORT, 'an intervention keeps the identity that produced it');
END;
