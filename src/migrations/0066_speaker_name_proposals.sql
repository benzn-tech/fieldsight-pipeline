-- A proposal is a row, not a render.
--
-- WHY THIS TABLE EXISTS AT ALL, which is not the obvious reason. It is not a convenience
-- layer over matching; it is the only supply line to the thing that makes matching work.
--
-- `recompute_company_floor` needs 20 rows of `source='correction'` before a company has a
-- rejection floor, and without a floor `decide_name` can never return `confirmed` (see
-- 0060 and the cap added alongside it). One rename gesture produces exactly ONE such row --
-- everything it propagates to is `correction_propagation`, which the floor calibration
-- deliberately excludes. So a company reaches a floor after twenty separate human
-- decisions, not twenty named passages, and at one decision per rename that is months.
--
-- Putting five judgements in front of someone at once is what turns months into a sitting.
--
-- THE STATE MACHINE, and the distinction the schema exists to protect:
--
--   pending    proposed, nobody has decided
--   confirmed  a human said yes -> the correction path runs -> one source='correction' row
--   rejected   a human said "this is not that person" -- information, never proposed again
--
-- **Closing the popup is none of these.** It leaves the row `pending`, and the bell shows
-- it later. Dismissal is not rejection: at one confirmation per gesture and twenty needed,
-- a mis-clicked close that silently consumed five candidates would set a company's
-- calibration back by a quarter with nothing anywhere recording that it happened. This
-- repository has already collapsed two different absences into one value once -- an empty
-- list meaning both "no filter" and "deny all" -- and paid for it.
--
-- There is deliberately NO admission threshold anywhere near this table. Candidates are
-- ranked by similarity and cut at top-N; ranking cannot be wrong and N is a screen-size
-- choice. Any column here that decided whether a candidate "is" a match would be the
-- absolute cut this line has refused to invent, wearing a different name.
CREATE TABLE IF NOT EXISTS speaker_name_proposals (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id      uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  -- Who is being proposed. ON DELETE CASCADE because a withdrawn voiceprint must not leave
  -- proposals behind that would re-offer the person a withdrawal was meant to remove.
  voiceprint_id   uuid NOT NULL REFERENCES speaker_voiceprints(id) ON DELETE CASCADE,
  -- WHICH PASSAGE, addressed the way `speaker_label_groups` addresses one: a (session,
  -- call, label) triple is a voice cluster, and the cluster is the unit a human can judge.
  -- A single turn is not -- three seconds out of context is what produced the one Phase 0
  -- miss, and asking somebody to rule on it invites the same error with their name on it.
  session_base    text NOT NULL,
  source_filename text NOT NULL,
  speaker_label   text NOT NULL,
  -- Carried on the row because `speaker_label_groups` does not have them and the correction
  -- path needs them: naming a passage goes through `speaker_corrections`, which addresses a
  -- session as (folder, date, session_base). Deriving them later would mean parsing an S3
  -- path or re-querying `recordings`, and the proposal's producer already has both in hand.
  --
  -- The alternative -- resolve at decision time -- was rejected because it makes answering
  -- a months-old proposal depend on data that may have moved since, and a proposal whose
  -- answer silently fails is worse than one that was never offered.
  user_folder     text NOT NULL,
  session_date    date NOT NULL,
  -- What it was ranked on. Stored so a decision can be argued with later, and because these
  -- are the numbers a future calibration would be fitted to -- the same reason
  -- `quarantine_score` is stored rather than recomputed.
  score           double precision,
  state           text NOT NULL DEFAULT 'pending'
                  CHECK (state IN ('pending', 'confirmed', 'rejected')),
  created_at      timestamptz NOT NULL DEFAULT now(),
  decided_at      timestamptz,
  decided_by      uuid REFERENCES users(id),
  -- Re-running candidate retrieval must not stack duplicates of a question already asked,
  -- and must not silently re-ask one already answered.
  UNIQUE (company_id, voiceprint_id, session_base, source_filename, speaker_label)
);

-- The one question the bell and the popup both ask: what is still waiting in this company.
-- Partial, because a decided proposal never appears in that answer again and carrying them
-- in the index would grow it without bound as the queue is used.
CREATE INDEX IF NOT EXISTS speaker_name_proposals_pending
  ON speaker_name_proposals (company_id, created_at DESC)
  WHERE state = 'pending';

COMMENT ON COLUMN speaker_name_proposals.state IS
  'pending | confirmed | rejected. Closing the popup is NOT a state: it leaves the row '
  'pending for the bell. Dismissal and rejection are different acts and collapsing them '
  'silently burns the human decisions a company''s rejection floor is calibrated from.';
