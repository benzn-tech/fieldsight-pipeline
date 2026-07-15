-- 0011: authority flip (unified-extraction Task 4 — spec §6).
-- topics.time_range/participants: display fields the extraction JSON already
-- carries (lambda_extract_session EXTRACTION_SCHEMA) but the Aurora boundary
-- dropped; needed so the org-api timeline shim can render the
-- daily_report.json shape from extraction-sourced topics.
-- topics.source: PASSIVE provenance column (default 'ai') — no reader/writer
-- yet; the correction loop (spec §8 Task 5) adds source='human' + delete
-- guards. Landed now so Task 5 needs no topics migration.
-- action_items.deadline_text: raw free-text deadline ("Tomorrow 08:00") the
-- date-typed column cannot hold (lambda_ingest._map_action_items nulls it).
ALTER TABLE topics ADD COLUMN time_range   text;
ALTER TABLE topics ADD COLUMN participants jsonb;
ALTER TABLE topics ADD COLUMN source       text NOT NULL DEFAULT 'ai';
ALTER TABLE action_items ADD COLUMN deadline_text text;
