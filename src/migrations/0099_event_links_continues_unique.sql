-- Cross-day item continuity (plan 2026-11-09, Task 1): one continues edge per (src, dst).
-- The continuity writer inserts with ON CONFLICT DO NOTHING on this index, so a re-run never
-- duplicates an edge. Partial: supported_by and the other link types stay non-unique.
CREATE UNIQUE INDEX IF NOT EXISTS ux_event_links_continues ON event_links (src_stable_id, dst_stable_id) WHERE link_type = 'continues';
