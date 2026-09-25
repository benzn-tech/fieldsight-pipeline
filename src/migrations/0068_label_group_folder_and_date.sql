-- Where and when the voice cluster was recorded, which the producer already knows.
--
-- `_rebind` reads `user_folder` and `date` off its request artifact on its first line and
-- uses them to fetch the audio. Then it stores neither -- the same shape as the centroid
-- in 0065, and with the same consequence: a question that could have been answered from
-- this row has to be answered somewhere else, or cannot be answered at all.
--
-- TWO THINGS ARE BLOCKED ON IT TODAY, and both were written up as gaps rather than fixed:
--
--   1. `speaker_name_proposals` requires `user_folder` and `session_date` NOT NULL, because
--      naming a passage goes through `speaker_corrections`, which addresses a session as
--      (folder, date, session_base). `candidates_for_person` returns a cluster's address
--      WITHOUT them, so a candidate could not be turned into a proposal at all.
--   2. Site narrowing. `label_group_candidates` accepts `site_id` and does not apply it,
--      and says so in its own docstring, because the site resolvers (`recordings.
--      site_for_media` / `site_for_day`) key on exactly these two values.
--
-- Deriving them instead was considered and refused. `session_base` carries the date, but
-- the folder is a DEVICE prefix -- `petros_pan_2026-09-22_...` against a real folder of
-- `Petros_Pan` -- and the two agree often enough to look like a rule and differ often
-- enough to be one. A guess that is usually right is the worst kind here: it fails on the
-- minority of sessions and looks like a data problem.
--
-- NULLABLE, like the centroid and for the same reason: the 167 rows written before this
-- have no folder and no date and never will. A reader treats NULL as "this row cannot be
-- addressed", never as "no folder" -- and in practice the same rows are the ones with no
-- centroid, so the candidate query already skips them.
ALTER TABLE speaker_label_groups
    ADD COLUMN IF NOT EXISTS user_folder  text,
    ADD COLUMN IF NOT EXISTS session_date date;

COMMENT ON COLUMN speaker_label_groups.user_folder IS
  'The recording owner''s folder, as the correction path addresses a session. NULL on rows '
  'written before 0068. NOT derivable from session_base: that carries a device prefix which '
  'usually but not always matches the folder.';
