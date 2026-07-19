-- 0018 (next free number; 0017 is the action_item audit): site coordinates for
-- weather + map features. Nullable -- existing rows predate coordinates and are
-- backfilled via Photon (non-VPC). Populated on create/edit by the UI's Photon
-- autocomplete pick; the in-VPC org-api only persists (never geocodes).
ALTER TABLE sites ADD COLUMN latitude  double precision;
ALTER TABLE sites ADD COLUMN longitude double precision;
