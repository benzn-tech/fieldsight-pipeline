-- 0047 — the homogeneity limit each sample was admitted under.
--
-- The limit is settable from the environment so TEST can get past a guard that refuses every
-- window of real site audio. That makes a new question askable and, until this column, not
-- answerable: which stored samples came in under a loosened guard?
--
-- It matters because of the property the guard exists for. A window holding two people
-- poisons a profile permanently — a profile cannot be un-poisoned, only the contributing
-- sample deleted, after somebody notices. The design already says so out loud. But the one
-- artifact that would let somebody notice lived only in a CloudWatch line that expires with
-- retention, so a profile built on TEST at 0.7 and a profile built at 0.35 were
-- indistinguishable the moment the logs aged out — including to whoever later decides
-- whether TEST data may be promoted or copied.
--
-- NULL means "admitted under whatever the default was at the time", which is every row
-- written before this column existed and every row written by a function with no override
-- set. Non-NULL is therefore exactly the set worth re-examining, and finding it is one query
-- rather than an archaeology exercise.
ALTER TABLE speaker_voiceprint_samples
    ADD COLUMN IF NOT EXISTS admitted_max_spread double precision;
