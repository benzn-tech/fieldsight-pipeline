-- 0038 — speaker identity: voiceprints, their samples, and the turns they named.
--
-- Design: docs/superpowers/specs/2026-08-09-speaker-identity-v2.md §6, §8
-- Gate:   docs/superpowers/specs/2026-08-11-speaker-phase0-results.md (Phase 0 passed:
--         two people five metres apart were told apart, 31 of 32 turns)
--
-- Ships INERT: the tables exist, nothing reads or writes them until Phase 4/5.
--
-- A voiceprint is biometric data under the NZ Privacy Act and the Biometric Processing
-- Privacy Code. Two consequences are baked into the schema rather than left to the code,
-- because both are un-reconstructable after the fact:
--
--   * consent belongs to the person RECORDED, not the person who typed the name, so it is
--     a column on the profile and a profile without it must never match;
--   * a withdrawal has to remove the vectors AND un-name everything they justified, which
--     is only enumerable if each sample points at the correction that produced it and each
--     named turn points at the profile that named it. Added later, that history is gone.
--
-- Forward-only, like every migration here. Reverting means writing 0039 that drops these
-- three tables — recorded now so it is not invented under pressure.

CREATE TABLE IF NOT EXISTS speaker_voiceprints (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id    uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  -- Nullable: a recurring unnamed voice may hold a profile before anyone names it, which
  -- is what makes "the same person again" visible without knowing who they are.
  user_id       uuid REFERENCES users(id) ON DELETE CASCADE,
  display_name  text,
  -- tentative | confirmed | withdrawn. `withdrawn` is a state rather than a deletion so
  -- the audit survives the data.
  status        text NOT NULL DEFAULT 'tentative',
  consent_at    timestamptz,
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS speaker_voiceprint_samples (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id     uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  voiceprint_id  uuid NOT NULL REFERENCES speaker_voiceprints(id) ON DELETE CASCADE,
  -- ECAPA-TDNN, 192 dimensions. One row per enrolment event rather than one averaged
  -- vector per person: §6's withdrawal needs each contribution individually removable, and
  -- an average cannot be un-poisoned.
  embedding      vector(192) NOT NULL,
  source         text NOT NULL,          -- correction | enrolment
  s3_key         text,                   -- the audio the vector came from
  window_start_s double precision,
  window_end_s   double precision,
  -- Who, and on the strength of what. Without these a bad enrolment can be deleted but not
  -- traced, and §6 asks for everything it justified to go with it.
  created_by     uuid REFERENCES users(id) ON DELETE SET NULL,
  correction_ref text,
  created_at     timestamptz NOT NULL DEFAULT now()
);

-- The provenance that makes a withdrawal enumerable. One row per turn that was given a
-- name. Without it, honouring a withdrawal means grepping S3 artifacts for a name — which
-- is not something anyone will do correctly under time pressure.
CREATE TABLE IF NOT EXISTS speaker_turn_names (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id     uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  voiceprint_id  uuid NOT NULL REFERENCES speaker_voiceprints(id) ON DELETE CASCADE,
  session_base   text NOT NULL,
  -- source_filename + start_sec: the same pair the evidence path already uses to point a
  -- quote at its audio, so a named turn and a cited quote refer to the same thing.
  turn_ref       text NOT NULL,
  -- confirmed | tentative. After a threshold change only one of these needs revisiting.
  state          text NOT NULL,
  score          double precision,
  margin         double precision,
  created_at     timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE speaker_voiceprint_samples ADD COLUMN IF NOT EXISTS correction_ref text;
ALTER TABLE speaker_voiceprint_samples ADD COLUMN IF NOT EXISTS created_by uuid;

CREATE INDEX IF NOT EXISTS idx_voiceprints_company
  ON speaker_voiceprints (company_id) WHERE status <> 'withdrawn';
CREATE INDEX IF NOT EXISTS idx_voiceprint_samples_profile
  ON speaker_voiceprint_samples (voiceprint_id);
CREATE INDEX IF NOT EXISTS idx_turn_names_profile
  ON speaker_turn_names (voiceprint_id);
CREATE INDEX IF NOT EXISTS idx_turn_names_session
  ON speaker_turn_names (company_id, session_base);
