-- 0016: editable action items (editable-tasks-reassignment spec §3.6) —
-- minimal last-writer audit, mirroring observations.updated_at. No
-- company_id (reached via action_items.site_id -> sites.company_id) and no
-- version column (last-write-wins). Both nullable: existing rows predate
-- any edit and correctly read NULL until first PATCHed.
ALTER TABLE action_items ADD COLUMN updated_at timestamptz;
ALTER TABLE action_items ADD COLUMN updated_by text;
