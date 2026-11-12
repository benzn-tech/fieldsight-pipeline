-- Decision card inbox (spec 2026-11-12-decision-card-inbox-design.md rev 2 §3).
-- Who answered a card, with which option, when and why. Nullable, no default, no backfill,
-- no index (lookups are by card_id PK). Options that are not decisions (remind, new date,
-- reassign) write no decision_ledger row, so the answer is recorded here.
-- Every statement is IF NOT EXISTS: the file is safe to execute again.
ALTER TABLE decision_cards
  ADD COLUMN IF NOT EXISTS answered_by uuid REFERENCES users(id),
  ADD COLUMN IF NOT EXISTS answered_option text,
  ADD COLUMN IF NOT EXISTS answered_at timestamptz,
  ADD COLUMN IF NOT EXISTS answer_reason text;
