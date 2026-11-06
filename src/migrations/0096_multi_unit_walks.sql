-- Multi-unit inspection walks (spec 2026-11-03): one opening ("pre-line check,
-- units 1 to 4") walks several units, possibly across recordings. A window
-- carries the units its opening named and the units he entered; walk_key ties
-- the day's stretches of one walk together -- built from (company, folder, day,
-- template, opening start_at), never inspection_windows.id, which every
-- re-extraction regenerates. NULL walk_key = a single-unit check, as before.
ALTER TABLE inspection_windows ADD COLUMN IF NOT EXISTS units jsonb;     -- {"names": [...], "count": n}
ALTER TABLE inspection_windows ADD COLUMN IF NOT EXISTS entered jsonb;   -- [{"unit","at","at_s","quote"}]
ALTER TABLE inspection_windows ADD COLUMN IF NOT EXISTS walk_key text;   -- NULL = single-unit check

-- One checklist report per walk, however many recordings it spans.
ALTER TABLE checklist_reports ADD COLUMN IF NOT EXISTS walk_key text;
CREATE UNIQUE INDEX IF NOT EXISTS uq_checklist_reports_walk
  ON checklist_reports (walk_key) WHERE walk_key IS NOT NULL;
-- Re-queueing a walk replaces its result_key. The results it replaced stay
-- readable while the new one is written, or when it failed (R39):
-- [{"key", "queued_at"}], newest first, at most 5. NULL = none.
ALTER TABLE checklist_reports ADD COLUMN IF NOT EXISTS previous_results jsonb;

-- Answers given on site at the final check (device or phone). Each one is
-- report evidence, stored with its transcript. A move ("that No was unit 3")
-- has no answer of its own but must name where it goes.
CREATE TABLE IF NOT EXISTS walk_answers (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id uuid NOT NULL REFERENCES companies(id),
  user_folder text NOT NULL, report_date date NOT NULL, walk_key text NOT NULL,
  unit text NOT NULL, item_text text NOT NULL,
  answer text CHECK (answer IN ('Yes','No','N/A')),          -- NULL when the row is a move
  comment text NOT NULL DEFAULT '', move_to_unit text,
  source text NOT NULL CHECK (source IN ('device','phone')),
  quote text NOT NULL DEFAULT '', voice_note_id uuid,
  answered_at text NOT NULL,                                 -- HH:MM:SS NZ, the day's clock
  answered_by uuid REFERENCES users(id),
  -- The question it answers (R36): an answer to a read-back No ('confirm_no') is
  -- the inspector's choice over the recording; only an answer to a not-checked
  -- question can disagree with the recording.
  asked_kind text NOT NULL DEFAULT 'not_checked'
    CHECK (asked_kind IN ('not_checked','confirm_no')),
  created_at timestamptz NOT NULL DEFAULT now(),
  CHECK (answer IS NOT NULL OR move_to_unit IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_walk_answers_walk ON walk_answers (walk_key, created_at);

-- The final check's open questions, as the device was given them (R41): what the
-- phone shows until a newer walk report exists. One row per final check with
-- questions; the newest one is read. Under the walk's company (R22).
CREATE TABLE IF NOT EXISTS walk_final_checks (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id uuid NOT NULL REFERENCES companies(id),
  user_folder text NOT NULL, report_date date NOT NULL, walk_key text NOT NULL,
  questions jsonb NOT NULL,                  -- [{"kind", "unit", "item_text", "comment"}]
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_walk_final_checks_walk ON walk_final_checks (walk_key, created_at);
