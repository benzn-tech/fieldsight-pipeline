-- A rejection floor under decide_name's margin, calibrated per company from that
-- company's own human-asserted corrections -- never authored as a number in source
-- (docs/superpowers/specs/2026-09-20-a-voice-the-library-does-not-know.md, S1).
--
-- decide_name's margin check alone confirms whoever is LEAST wrong among the enrolled
-- profiles, even when nobody enrolled was in the room: measured on 2026-09-10, a
-- stranger was confirmed at best=0.445, clear of the runner-up by 0.268 -- comfortably
-- past the 0.15 margin. A floor asks a second question the margin cannot: is the
-- winning score itself high enough to mean anything, for THIS company's own history.
--
-- One row per company, recomputed by a scheduled job rather than at match time -- the
-- floor must not move mid-decision because one turn happened to land during
-- recomputation (matching the existing pattern for other derived/materialized state,
-- e.g. programmes/{site_id}/programme.json regenerated from Aurora).
--
-- sample_count is the count of source='correction' speaker_turn_names rows the floor
-- was built from, kept beside the floor rather than only in a log line: it is what lets
-- decide_name's caller (and an operator watching this table) tell "no floor because too
-- little evidence" apart from "no floor because nobody ran the job yet", and it is the
-- standing metric the spec's own risk table asks to be watched (S7, last row).
--
-- No row at all, rather than a row with floor=NULL, for a company below the minimum: a
-- present NULL and an absent row would both have to be treated as "no floor" by every
-- reader, and only one of them is enforced by "SELECT ... WHERE company_id = %s
-- returning nothing" -- a NULL floor is a value a future bug could compare against.
CREATE TABLE IF NOT EXISTS speaker_voiceprint_company_floors (
    company_id    uuid NOT NULL PRIMARY KEY,
    floor         double precision NOT NULL,
    sample_count  integer NOT NULL,
    computed_at   timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE speaker_voiceprint_company_floors IS
  'One row per company: a low percentile of that company''s own source=''correction'' '
  'decide_name "best" scores, recomputed by a scheduled job. No row = below the minimum '
  'sample count to calibrate responsibly; decide_name then applies no floor, exactly as '
  'every company does today.';
COMMENT ON COLUMN speaker_voiceprint_company_floors.sample_count IS
  'How many source=''correction'' rows the floor was built from. Tracked so a company '
  'stuck below the minimum for longer than expected is visible directly from this table, '
  'with no new instrumentation.';
