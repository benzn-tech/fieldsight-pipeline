-- When the sweep CLAIMED a session, so the confirmation email can be waited for.
--
-- The email is moving behind the session's final extraction (spec 2026-09-16):
-- item-writer enqueues it once the rows are durable, and the sweep keeps a
-- backstop for the sessions whose final never arrives -- no turns, no API key,
-- a raise that exhausted its S3 retries, or a folder-less login.
--
-- That backstop needs a clock, and the obvious one is wrong. `closed_at` for an
-- idle-inferred close is the session's LAST ACTIVITY (lambda_finalize_claim's
-- infer_idle_closes marks pending_close at last_activity), which is already
-- SESSION_GAP_MINUTES old by the time anything claims it -- so a backstop
-- measured from the close would fire on the very next tick, before any
-- extraction could land, for exactly the offline and crashed recordings this
-- feature exists for.
--
-- `updated_at` would work today: claim_finalize sets it, and nothing else
-- writes a `finalizing` row. That is a property of the current code, not a
-- promise -- the next writer of updated_at would silently move every deadline.
-- Hence a column that means one thing.
--
-- NULL for rows claimed before this shipped. The backstop treats NULL as "no
-- anchor" and leaves those to reconcile, rather than inventing a deadline for a
-- session nobody is waiting on any more.
ALTER TABLE meeting_session
    ADD COLUMN IF NOT EXISTS finalizing_at timestamptz;

-- The backstop scans `finalizing` rows every minute. Partial: the table is
-- overwhelmingly sent/failed, and only the claimed ones are ever looked at.
CREATE INDEX IF NOT EXISTS idx_meeting_session_finalizing_at
    ON meeting_session (finalizing_at)
    WHERE status = 'finalizing';
