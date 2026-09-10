-- An unanswered question is not a summary, and not an action.
--
-- Meetings produce them: "is 3604 a 150 or a 200 -- I'll have to check". Until
-- now `lambda_meeting_minutes` glued them onto the end of the topic summary
-- ("... Open questions: a; b; c"), which is how they reached this table at all
-- -- as prose, inside another field.
--
-- That concatenation is what the report's owner objected to on 2026-09-10: it
-- buries both halves. The report now carries them as their own key and renders
-- them as their own section, and the moment it did, this table became the place
-- they were lost: `topics` has a column for a summary, for actions, for safety,
-- and had nowhere for a question. So the Timeline -- which reads Aurora, not
-- the report file -- would have shown a day's questions before this change and
-- none after it. Fixing the report by deleting information from the timeline is
-- not a fix.
--
-- jsonb, not text[]: same convention as `participants` and `evidence` on this
-- table, and it leaves room for a question to gain a field (who asked, what it
-- blocks) without a second migration.
--
-- NULL means "this extraction predates the column or was never asked", which is
-- not the same as `'[]'` -- asked and there were none. Every existing row is
-- NULL and that is the honest value for them.
ALTER TABLE topics ADD COLUMN IF NOT EXISTS open_questions jsonb;

COMMENT ON COLUMN topics.open_questions IS
  'Questions raised and left unanswered in this topic. NULL = not captured; [] = none raised.';
