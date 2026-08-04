-- Device ledger (spec docs/superpowers/specs/2026-08-03-device-management-design.md).
--
-- asset_tag (the physical FS-xx label stuck on the device) is the AUTHORITATIVE
-- identity. device_uuid is advisory only: the F2SP devices are likely flashed
-- from one factory image, so ANDROID_ID may be identical across all of them,
-- and this ROM already misreports SENSOR_ORIENTATION. When one uuid is seen
-- under two tags, uuid_trusted is cleared and never recovers.
--
-- No status column and no assignment table on purpose. Status is derived from
-- last_seen_at against a hand-entered dispatch date; assignment originates from
-- a human and lives in Notion. Storing either here would create a second source
-- of truth for data that only ever comes from outside the system.

create table if not exists devices (
  id                uuid primary key,
  asset_tag         text unique not null,
  device_uuid       text,
  uuid_trusted      boolean not null default true,
  app_version       text,
  last_seen_at      timestamptz,
  last_account_sub  text,
  created_at        timestamptz not null default now()
);

-- The ledger is read "quietest first": never-seen rows are the alert that
-- matters most, so nulls sort to the top.
create index if not exists devices_last_seen_idx
  on devices (last_seen_at nulls first);

create index if not exists devices_uuid_idx
  on devices (device_uuid) where device_uuid is not null;

alter table recordings add column if not exists device_id uuid;

do $$
begin
  if not exists (
    select 1 from pg_constraint where conname = 'recordings_device_id_fkey'
  ) then
    alter table recordings
      add constraint recordings_device_id_fkey
      foreign key (device_id) references devices(id);
  end if;
end $$;

-- Serves "which site did this device actually work at" — the newest recording
-- per device.
create index if not exists recordings_device_created_idx
  on recordings (device_id, created_at desc);
