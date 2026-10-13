-- 0076: the extractor's lineage id. Spec 2026-09-30 (the extractor says which item continues which), D1.
-- stable_id stays owned by the database and is moved by carry-forward; item_id is assigned by
-- lambda_extract_session in code and inherited across passes when the model's continuity claim passes
-- the guards. Two ids, one owner each: reusing stable_id for the extractor's lineage breaks the chain
-- the first time an item is carried by text (final review C1).
-- Nullable, no default, no index: no table rewrite (contrast 0073's volatile default, Track B R20);
-- carry-forward reads these rows by topic id, never by item_id.
ALTER TABLE action_items    ADD COLUMN IF NOT EXISTS item_id uuid;
ALTER TABLE findings        ADD COLUMN IF NOT EXISTS item_id uuid;
ALTER TABLE topic_decisions ADD COLUMN IF NOT EXISTS item_id uuid;
ALTER TABLE topic_questions ADD COLUMN IF NOT EXISTS item_id uuid;
