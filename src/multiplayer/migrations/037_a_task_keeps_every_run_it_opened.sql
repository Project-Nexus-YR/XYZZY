-- Which task a run answers belongs on the run, because a task has many runs.
--
-- 035 recorded the link the other way round, as agent_tasks.execution_id, and
-- that column holds one execution: attach_execution overwrites it every time
-- start_agent_task opens another turn, which it legally does out of
-- INPUT_REQUIRED and out of AUTH_REQUIRED. The overwritten execution is still
-- PENDING — open, claimable, dispatchable — and the arm of bounding_principals
-- that reads a delegated run's chain joined through that pointer. So the
-- delegator's ceiling evaporated from a live run the moment a second turn
-- opened on the same task: relocation sixteen, the same defect in another
-- costume, caused by writing a many-to-one relationship as a one-slot pointer.
--
-- agent_tasks.execution_id stays as the newest turn, which is what a reader
-- asking "what is this task doing now" wants. It is no longer what any
-- authority derivation reads.
ALTER TABLE executions ADD COLUMN agent_task_id TEXT REFERENCES agent_tasks(task_id);

-- The bound is derived at every spend, so the join behind it is a hot path.
CREATE INDEX idx_executions_agent_task ON executions(agent_task_id);
