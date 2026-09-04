-- What a run spent.
--
-- Both providers report token_usage on every turn and /metrics already counts
-- it into xyzzy_model_tokens_total, but nothing stores it per row, so nobody
-- can answer what one branch cost. The two rows a turn or a synthesis settles
-- get a column each; a row from before this migration reads zero, which is
-- the honest answer for spend nothing here ever measured.

ALTER TABLE executions ADD COLUMN token_usage INTEGER NOT NULL DEFAULT 0;

ALTER TABLE branch_syntheses ADD COLUMN token_usage INTEGER NOT NULL DEFAULT 0;
