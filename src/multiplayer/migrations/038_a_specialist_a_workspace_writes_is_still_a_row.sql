-- A specialist a workspace writes is still a row, not a redeploy.
--
-- agent_templates has held only the four built-ins since 001: seeded once at
-- startup from hardcoded strings a developer wrote, never from a member. A
-- workspace that wants its own specialist had no row to write one into, and
-- no column would have said whose it was or which workspace it belonged to.
--
-- workspace_id null means built-in, the same convention this schema already
-- uses elsewhere for "nobody owns this, everybody can read it." created_by
-- names who wrote a workspace's own row, for the creator-or-admin check its
-- deletion needs. deleted_at is a written fact rather than a removed row: an
-- agent_instance already spawned from a template copies the template's fields
-- onto itself at spawn (service.py, spawn_agent), so the row a DELETE retires
-- is never read by anything that agent does afterward, but the FK from
-- agent_instances.template_id — installed in 001 and still enforced — would
-- refuse to let the row disappear out from under it. Marking it deleted keeps
-- that FK honest and keeps the row an already-spawned agent's provenance can
-- still point to, while removing it from anything a new spawn or a listing
-- would surface.
ALTER TABLE agent_templates ADD COLUMN workspace_id TEXT REFERENCES workspaces(workspace_id);
ALTER TABLE agent_templates ADD COLUMN created_by TEXT;
ALTER TABLE agent_templates ADD COLUMN deleted_at TEXT;

-- Listing a workspace's templates is a per-workspace read; nothing else queries by it.
CREATE INDEX idx_agent_templates_workspace ON agent_templates(workspace_id);
