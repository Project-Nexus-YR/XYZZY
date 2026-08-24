-- An agent may ask another agent, and the asking is a task with a state.
--
-- Until now an agent ran because a human mentioned it. Delegation is the same
-- act with a different asker, and the whole difficulty is that the asker is not
-- a person: whatever authority the delegate spends has to come from the human
-- who authorised the delegator, never from the delegator itself.
--
-- So this table records who asked and under which run, and records nothing
-- about what either of them was allowed to do. The tenth relocation of this
-- codebase's oldest defect was caused by persisting a capability set: the set
-- froze while the durable rows behind it moved, and a narrowed principal kept
-- spending what they used to hold. Terms are re-derived at the moment of
-- spending, from rows, every time. There is deliberately no column here for
-- them, because a column is an invitation to read it instead.
--
-- The state vocabulary is Google's A2A, verbatim, including the two
-- interruptible states. INPUT_REQUIRED is the delegate needing more from the
-- asker; AUTH_REQUIRED is the delegate needing a capability nobody in the chain
-- can lend it, which is where a named human is asked for that one escalation.
-- A protocol built for agents across organisations means "go authenticate" by
-- that state. Here it means a person is being asked, by name, for one thing.
--
-- agent_task_chain is the ancestry of a task, one row per ancestor. A workspace
-- where five agents share a room will produce a cycle the first afternoon
-- somebody wires two agents to consult each other, and a cycle is a refusal
-- rather than a stack overflow. The chain is also what bounds depth.

CREATE TABLE agent_tasks (
    task_id TEXT PRIMARY KEY,
    context_id TEXT NOT NULL,
    room_id TEXT NOT NULL,
    target_agent_id TEXT NOT NULL,
    delegating_agent_id TEXT,
    delegating_run_id TEXT,
    execution_id TEXT,
    state TEXT NOT NULL,
    accepted_output_modes TEXT NOT NULL DEFAULT '[]',
    depth INTEGER NOT NULL DEFAULT 0,
    authorized_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    terminal_at TEXT,
    refusal_reason TEXT NOT NULL DEFAULT ''
);

CREATE INDEX idx_agent_tasks_room ON agent_tasks(room_id);
CREATE INDEX idx_agent_tasks_context ON agent_tasks(context_id);
CREATE INDEX idx_agent_tasks_target ON agent_tasks(target_agent_id, state);
CREATE INDEX idx_agent_tasks_execution ON agent_tasks(execution_id);

-- One row per ancestor, so "is this agent already in this chain" is a query
-- rather than a walk, and so depth is a count rather than a claim.
CREATE TABLE agent_task_chain (
    task_id TEXT NOT NULL REFERENCES agent_tasks(task_id),
    position INTEGER NOT NULL,
    agent_id TEXT NOT NULL,
    PRIMARY KEY (task_id, position)
);

-- UNIQUE, because the query this index exists to serve is asked in order to
-- refuse a cycle, and a table that can hold one has already lost the argument:
-- the chain would answer "yes, twice" to a question whose only safe answer is
-- "no". The refusal belongs in the repository, and this is the backstop that
-- makes writing one impossible rather than merely discouraged.
CREATE UNIQUE INDEX idx_agent_task_chain_agent ON agent_task_chain(task_id, agent_id);

-- A message is a role and an ordered list of typed parts, which is A2A's model
-- and not this codebase's existing Message: that one belongs to a human channel
-- and carries mentions, reactions and read cursors. Sharing a table would mean
-- one of the two grows columns that never apply to it.
CREATE TABLE agent_task_messages (
    message_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES agent_tasks(task_id),
    sequence INTEGER NOT NULL,
    role TEXT NOT NULL,
    parts TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (task_id, sequence)
);

CREATE INDEX idx_agent_task_messages_task ON agent_task_messages(task_id, sequence);
