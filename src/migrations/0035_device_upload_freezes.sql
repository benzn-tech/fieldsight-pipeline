-- Upload freeze register (spec GrandTime docs/superpowers/specs/2026-08-06-upload-freeze-thaw-design.md).
--
-- A recording that cannot upload because of something only we can fix is frozen rather than
-- retried: retrying a mis-mapped identity does not fix a mis-mapped identity, and the seven
-- days it used to spend trying were pure delay before a silent loss.
--
-- The table is created in Phase 1 and stays EMPTY until Phase 2, so that promoting the
-- feature later adds no migration to an already-busy deploy. Only the three `devices`
-- columns are read and written in Phase 1.

create table if not exists device_upload_freezes (
  device_id         uuid not null references devices(id),
  -- The failure fingerprint the device reports, e.g. `uploadurl_403`. Deliberately the
  -- device's own string: the ledger groups on what the device saw, not on our guess at it.
  fingerprint       text not null,
  first_seen_at     timestamptz not null default now(),
  last_seen_at      timestamptz not null default now(),
  record_count      integer not null default 0,
  observed_build    text,
  -- Stamped when the operator is first told, so a freeze that is already known and awaiting
  -- a fix is not pushed again every run. Repetition is how a channel gets ignored.
  first_notified_at timestamptz,
  thaw_requested_at timestamptz,
  thaw_requested_by text,
  -- Set when the DEVICE stops reporting the fingerprint, NOT when the instruction is sent.
  -- A response lost in flight must not discard the operator's decision — the same
  -- client-gave-up-after-the-server-committed asymmetry that left 69 objects in S3 with
  -- their rows never completed on 2026-08-03.
  thawed_at         timestamptz,
  primary key (device_id, fingerprint)
);

create index if not exists device_upload_freezes_open_idx
  on device_upload_freezes (device_id) where thawed_at is null;

-- Backlog vitals live on the device row: one number per device, overwritten each report.
-- Age rather than count — a count is diluted by every new recording, an age is monotonic
-- and comparable across devices.
alter table devices add column if not exists backlog_oldest_age_s bigint;
alter table devices add column if not exists backlog_pending      int;
alter table devices add column if not exists backlog_reported_at  timestamptz;
