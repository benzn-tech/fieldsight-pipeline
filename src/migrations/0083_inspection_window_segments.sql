-- An interrupted check is several stretches of one check (owner, 2026-10-05):
-- [{"from": "HH:MM:SS", "to": "HH:MM:SS"}], in order. start_at/end_at span
-- them all. NULL on rows written before this -- read as one stretch.
ALTER TABLE inspection_windows ADD COLUMN IF NOT EXISTS segments jsonb;
