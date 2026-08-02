-- 0029: a dated snapshot of the task set as it stood at each import.
--
-- Closes a gap in 0027 that only became visible when Project 2 needed
-- "落后多少天". programme_tasks.start_date/end_date are overwritten IN PLACE by
-- every import, and first_seen_version / removed_in_version record only which
-- tasks EXISTED at version N — not what their dates were. So lateness against
-- a baseline could not be computed at all unless the baseline happened to be
-- the current version, which is the one case where the number is always zero.
--
-- Stored at import commit, which is the only moment the data is certainly
-- correct. Compact on purpose: [{i: source_task_id, s: start, e: end, d: days}].
-- A 5,000-task programme is roughly 200KB, and versions arrive monthly.
ALTER TABLE programme_versions
  ADD COLUMN task_snapshot jsonb NOT NULL DEFAULT '[]'::jsonb;
