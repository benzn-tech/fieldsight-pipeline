-- 0028: per-user UI preferences.
--
-- The programme time window has to follow a user between their office
-- desktop and the site tablet (spec §7), which rules out localStorage.
--
-- jsonb rather than columns because these are UI choices with no referential
-- meaning and will accrete; writes shallow-merge (`prefs || %s`) so a surface
-- saving its own key cannot clobber one it never read.
ALTER TABLE users ADD COLUMN prefs jsonb NOT NULL DEFAULT '{}'::jsonb;
