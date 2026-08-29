-- 0051 — one session, one speaker namespace.
--
-- Batching transcribes a session as ~14 separate ASR calls and each numbers its speakers from
-- scratch, so `spk_0` means a different person in 6 of them. Measured on a real 26-minute
-- meeting: 50.2 % purity on the biggest label, an almost perfect coin flip between two people
-- (docs/superpowers/specs/2026-08-29-asr-accuracy-measured-findings.md).
--
-- This table holds the correction: which (call, label) pairs are the same voice, decided by
-- local ECAPA centroids rather than by a second ASR pass.
--
-- A MAPPING, NOT A REWRITE. The transcript artifact is untouched, for the three reasons
-- `turn_name_overlay` already documents: a re-run of extraction rewrites the artifact and an
-- overlay survives it; a derived document with two writers is a defect this repository has
-- paid for; and a mapping can be withdrawn by deleting rows.
--
-- `group_label` is a LETTER within one session -- 'A', 'B'. It identifies nobody, no vector is
-- stored, and no consent is needed. Names are a different feature with different rules, and
-- group 'A' on Monday has nothing to do with group 'A' on Tuesday.
--
-- `spread` is kept because a group nobody can audit is a group nobody can withdraw. The
-- failure worth catching is not a missing group -- that reads exactly as today -- but a
-- confidently WRONG one: two people merged read as one person for a whole session, which is
-- worse than an obviously inconsistent "Speaker 1".
--
-- `speaker_label` is NOT NULL and part of the key, so undiarised segments (where the label is
-- NULL) simply have no group and fall back, which is the correct behaviour rather than an
-- omission.

CREATE TABLE IF NOT EXISTS speaker_label_groups (
  company_id      uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  session_base    text NOT NULL,
  -- The transcript basename EXACTLY as the read path spells it, `.json` included. A writer
  -- that stores the `.wav` spelling produces zero groups, zero errors and a clean fallback --
  -- indistinguishable from "the re-bind has not run". Pinned by a seam test, not by hope.
  source_filename text NOT NULL,
  speaker_label   text NOT NULL,
  group_label     text NOT NULL,
  spread          real,
  created_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (company_id, session_base, source_filename, speaker_label)
);

-- The read path asks one question per transcript view: every group for this session. Without
-- this the answer is a scan of every group row in the company on a page load.
CREATE INDEX IF NOT EXISTS speaker_label_groups_session
  ON speaker_label_groups (company_id, session_base);
