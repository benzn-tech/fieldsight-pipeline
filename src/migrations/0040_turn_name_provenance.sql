-- 0040 — what a turn's name came from, and whether it still stands.
--
-- 0038 created `speaker_turn_names` for Phase 5 matching, where every row is justified by a
-- stored profile. Correction propagation writes rows justified by a CORRECTION instead, and
-- the table cannot express that:
--
--   * no `source`, so a machine-produced name is indistinguishable from a human one — which
--     matters more than it sounds, see the confirmations note below;
--   * no pointer to the correction, so withdrawal cannot enumerate what a correction
--     justified, which §6 requires it to do;
--   * no supersession, so re-running a session accumulates answers instead of replacing them;
--   * `voiceprint_id NOT NULL`, which a correction that creates no profile cannot satisfy —
--     and creating a named profile just to satisfy the FK is exactly the consent violation
--     Phase 4 refuses.
--
-- Numbered 0040, not 0039: 0038's own header reserves 0039 for the migration that drops its
-- three tables, and quietly taking that number would remove a recorded rollback path. The
-- runner sorts by parsed version and does not require contiguity.
--
-- Spec: docs/superpowers/specs/2026-08-13-speaker-correction-propagation.md
-- Plan: docs/superpowers/plans/2026-08-13-correction-propagation-implementation.md (P2)

ALTER TABLE speaker_turn_names ALTER COLUMN voiceprint_id DROP NOT NULL;

-- correction | correction_propagation | match. The one column that stops the system
-- confirming its own profiles: `confirmations_count` counts confirmed rows as INDEPENDENT
-- HUMAN confirmations, and without a source it would count machine-propagated ones too.
ALTER TABLE speaker_turn_names ADD COLUMN IF NOT EXISTS source text;
ALTER TABLE speaker_turn_names ADD COLUMN IF NOT EXISTS correction_ref text;

-- Which session-local voice this turn was grouped into. Not a person: an unnamed cluster is
-- a real answer ("someone consistent, not identified") and must never reach a user-visible
-- surface as a name.
ALTER TABLE speaker_turn_names ADD COLUMN IF NOT EXISTS cluster_ref text;

-- The provider's speaker label disagreed with the voice grouping. Recorded rather than
-- resolved: two independent sources contradicting each other is where a confident name is
-- least warranted, so the row caps at tentative and says why.
ALTER TABLE speaker_turn_names ADD COLUMN IF NOT EXISTS label_disagreement boolean;

-- The clustering threshold this row was produced under. Frozen at 0.85 (Gate A, 2026-08-13),
-- and expected never to change — the column exists so that if it ever does, every row not
-- carrying the current value is disqualified from calibration automatically rather than by
-- somebody remembering.
ALTER TABLE speaker_turn_names ADD COLUMN IF NOT EXISTS cluster_threshold double precision;

-- Marked, never deleted: the audit of a correction is partly the record of what it undid.
ALTER TABLE speaker_turn_names ADD COLUMN IF NOT EXISTS superseded_at timestamptz;

-- One live answer per turn. A backstop, NOT the guarantee: the read-time join matches turns
-- by overlap with tolerance, because re-extraction shifts start_sec and a strict join would
-- make names silently vanish. Two rows whose turn_ref strings differ slightly can therefore
-- both match one physical turn while satisfying this index, so precedence is applied again
-- at read.
CREATE UNIQUE INDEX IF NOT EXISTS idx_turn_names_live
  ON speaker_turn_names (company_id, session_base, turn_ref)
  WHERE superseded_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_turn_names_correction
  ON speaker_turn_names (correction_ref) WHERE correction_ref IS NOT NULL;
