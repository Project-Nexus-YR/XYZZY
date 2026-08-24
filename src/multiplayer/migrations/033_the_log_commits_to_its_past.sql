-- The log commits to its past.
--
-- room_events was ordered and durable but not tamper-evident: a row could be
-- edited, removed or reordered after the fact and nothing downstream would
-- notice, because nothing downstream held a commitment to what the log said
-- before. Each event now stores the hash of the previous event and a hash of
-- its own stored fields chained onto it, so a quiet rewrite is impossible:
-- changing any row breaks every hash after it, and truncating the tail leaves
-- the room's sequence counter pointing past the end.
--
-- Both columns are filled by the append path for new rows and by the startup
-- backfill for rows written before this migration; the SQL layer cannot
-- compute a digest, so here they only exist.

ALTER TABLE room_events ADD COLUMN prev_hash TEXT;
ALTER TABLE room_events ADD COLUMN event_hash TEXT;
