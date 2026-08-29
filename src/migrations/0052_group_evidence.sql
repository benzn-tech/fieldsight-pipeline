-- 0052 — how much evidence a speaker group was built on.
--
-- 0051 stored `spread` on the reasoning that "a group nobody can audit is a group nobody can
-- withdraw". Measured on the first four real sessions: **9 of 12 rows have spread NULL**,
-- including every row of the two single-speaker sessions.
--
-- `frame_spread` returns None for fewer than two vectors, and most (call, label) pairs
-- contribute exactly one turn ≥ 3 s. So the audit column was empty for three quarters of the
-- rows, and the claim it was added for was true of a quarter of them.
--
-- `spread` answers "did the evidence disagree with itself", which needs at least two turns.
-- These two answer "how much evidence is there", which is always answerable and is the
-- question somebody actually asks when a group looks wrong:
--
--   turns    how many speaker turns went into the centroid
--   seconds  how much audio they came to
--
-- A group built on one 4-second turn and a group built on six turns totalling 90 seconds are
-- different claims, and until now the table recorded them identically.
--
-- Nullable rather than NOT NULL DEFAULT 0: the rows written before this migration genuinely
-- do not know, and a zero would say "no evidence" about groups that in fact had some. "We did
-- not record it" and "there was none" are different facts -- the distinction this whole
-- feature keeps being caught by.

ALTER TABLE speaker_label_groups
  ADD COLUMN IF NOT EXISTS turns   int,
  ADD COLUMN IF NOT EXISTS seconds real;
