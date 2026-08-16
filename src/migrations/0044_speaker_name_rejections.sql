-- 0044 — a name that was taken off a session, remembered.
--
-- `unname` supersedes the rows and records nothing about WHY. That was harmless while every
-- name came from a person: nobody re-derives a name a person deleted.
--
-- Label inheritance breaks that. It derives names from the transcriber's own grouping, so a
-- user says "that is not me", the rows go, and the next run re-derives the same name from
-- the same label and writes it back — with no log line anywhere, and no way for the user to
-- make it stop except by deleting it again after every run.
--
-- Superseding is not enough on its own: a superseded row records that a name WAS shown, not
-- that it was REJECTED. Those are different facts and only one of them should stop a later
-- inference.
--
-- Scoped to one session, matching `unname` itself: a person may be named correctly in twenty
-- meetings and wrongly in one, and a rejection here must not follow them everywhere.
CREATE TABLE IF NOT EXISTS speaker_name_rejections (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id   uuid NOT NULL,
    session_base text NOT NULL,
    display_name text NOT NULL,
    rejected_by  uuid,
    rejected_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (company_id, session_base, display_name)
);

CREATE INDEX IF NOT EXISTS idx_speaker_name_rejections_session
    ON speaker_name_rejections (company_id, session_base);
