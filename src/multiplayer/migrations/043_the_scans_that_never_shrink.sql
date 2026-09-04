-- The scans that never shrink.
--
-- Three tables are append only in practice, nothing in src prunes them, and
-- each backs a query that runs on a hot path without an index that leads
-- with the column the query actually filters on. Every one of these queries
-- gets slower with every row the product ever writes, and none of them get
-- faster again.
--
-- ontology_reviews already indexes (target_type, target_id, created_at) for
-- "the history of one entity," but get_room_ontology and the per-assertion
-- latest_review lookup both filter by room_id first. agent_tasks already
-- indexes room_id, context_id, target_agent_id and execution_id, but the
-- startup stale-task sweep filters by state and orders by created_at, which
-- none of those serve. executions already indexes session_id, but the
-- steer path and the post-restart fallback look up the newest run for one
-- agent_id, which that index does not serve either.

CREATE INDEX IF NOT EXISTS idx_ontology_reviews_room_target_created
    ON ontology_reviews(room_id, target_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_agent_tasks_state_created
    ON agent_tasks(state, created_at);

CREATE INDEX IF NOT EXISTS idx_executions_agent_status_started
    ON executions(agent_id, status, started_at DESC);
