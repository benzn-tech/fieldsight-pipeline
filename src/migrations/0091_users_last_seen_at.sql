-- 0091: users.last_seen_at -- the Team page's "Last seen" column.
--
-- Stamped by GET /api/org/me (lambda_org_api._stamp_last_seen), throttled in SQL to
-- at most one write per user per 5 minutes. NULL = never seen since this shipped;
-- there is deliberately no backfill (no trustworthy source for a past value).
-- Idempotent: safe to run twice.
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_seen_at timestamptz;
