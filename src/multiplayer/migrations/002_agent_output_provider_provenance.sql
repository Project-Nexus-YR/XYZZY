-- Exact, immutable provider-request provenance. Defaults preserve historical
-- AgentOutput rows while making the absence of legacy evidence explicit.
ALTER TABLE agent_outputs ADD COLUMN provider_input TEXT NOT NULL DEFAULT '';
ALTER TABLE agent_outputs ADD COLUMN provider_name TEXT NOT NULL DEFAULT '';
ALTER TABLE agent_outputs ADD COLUMN provider_model TEXT NOT NULL DEFAULT '';
ALTER TABLE agent_outputs ADD COLUMN provider_response_id TEXT NOT NULL DEFAULT '';
ALTER TABLE agent_outputs ADD COLUMN provider_interventions TEXT NOT NULL DEFAULT '[]';
ALTER TABLE agent_outputs ADD COLUMN provider_evidence TEXT NOT NULL DEFAULT '';
