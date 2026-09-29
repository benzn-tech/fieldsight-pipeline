-- A self-introduction the transcript overheard, waiting for a human to say yes or no.
--
-- WHY THIS IS A SEPARATE TABLE, not another row shape in `speaker_name_proposals`
-- (0066). That table asks "is this <existing person>?" -- it exists to produce
-- `source='correction'` rows against an ALREADY-ENROLLED `voiceprint_id`, and
-- `voiceprint_id` there is `NOT NULL` on purpose: a proposal with nobody to propose is a
-- row no interface can render. This table asks the opposite question, "who is this NEW
-- voice?", about a passage nothing has ever seen before -- there is no profile to point
-- at yet, which is exactly why it needs the words (`heard_name`, `quote`) that
-- `speaker_name_proposals` does not carry at all. Folding the two into one table would
-- mean either a `voiceprint_id` that is sometimes required and sometimes not (a state
-- machine encoded as nullability, the same shape this repository's `= NULL` note already
-- warns about) or a name column nobody reads on the other path.
--
-- THE STATE MACHINE mirrors 0066's, for the same reason:
--
--   pending    the detector heard a name, nobody has answered
--   confirmed  a human said "yes, that's a name" -> speaker_corrections runs -> one
--              source='correction' row, the SAME calibration currency 0066 exists for
--   rejected   a human said "not a name" (or the wrong person) -- information, never
--              re-offered
--
-- Closing the dialog is none of these and must leave the row `pending`, same rule,
-- same reason: a mis-click must not silently burn one of the twenty human decisions a
-- company's rejection floor is calibrated from.
CREATE TABLE IF NOT EXISTS speaker_intro_suggestions (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id      uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  -- Addressed the way `speaker_name_proposals` addresses a cluster: (session, call,
  -- label). `session_base` is the canonical key (`turn_name_overlay.session_base`,
  -- `sid<32hex>`); `source_filename`/`speaker_label` are the (call, label) pair inside it.
  session_base    text NOT NULL,
  source_filename text NOT NULL,
  speaker_label   text NOT NULL,
  -- Carried on the row for the same reason 0066 carries them: `speaker_corrections`
  -- addresses a session as (folder, date, session_base), and resolving them at decision
  -- time would make answering an old suggestion depend on data that may have moved.
  user_folder     text NOT NULL,
  session_date    date NOT NULL,
  start_sec       double precision NOT NULL,
  end_sec         double precision NOT NULL,
  -- What was actually heard, plain words, no score anywhere on this row (the customer
  -- rule the design doc states up front). The transcriber gets names wrong ("Petros Pan"
  -- -> "Petrus Pang", 2026-09-22) -- `heard_name` is a suggestion, never written to
  -- `speaker_voiceprints` until a human confirms it, possibly under an edited spelling.
  heard_name      text NOT NULL,
  company_name    text,
  quote           text NOT NULL,
  state           text NOT NULL DEFAULT 'pending'
                  CHECK (state IN ('pending', 'confirmed', 'rejected')),
  decided_by      uuid REFERENCES users(id),
  decided_at      timestamptz,
  created_at      timestamptz NOT NULL DEFAULT now(),
  -- Re-running the writer over the same extraction (a re-extraction, the finalize sweep's
  -- re-run chain, org-api's regenerate) must not stack duplicate questions about a voice
  -- already asked about. `ON CONFLICT ... DO NOTHING` is the whole design of the repository
  -- function that inserts against this, mirroring 0066's own callout: a `WHERE NOT EXISTS`
  -- in Python is not this table's contract, this constraint is.
  UNIQUE (company_id, session_base, source_filename, speaker_label)
);

-- The bell's question and the list's question are both "what is still waiting for THIS
-- company" -- partial, same as 0066's index, so a decided suggestion never grows an index
-- that only the pending count and the pending list ever read.
CREATE INDEX IF NOT EXISTS speaker_intro_suggestions_pending
  ON speaker_intro_suggestions (company_id, created_at DESC)
  WHERE state = 'pending';

COMMENT ON COLUMN speaker_intro_suggestions.state IS
  'pending | confirmed | rejected. Closing the dialog is NOT a state: it leaves the row '
  'pending for the bell. Dismissal and rejection are different acts, same as '
  'speaker_name_proposals.state.';
