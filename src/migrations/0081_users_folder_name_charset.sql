-- A recording folder is an S3 path segment: [A-Za-z0-9._-] only (spec 2026-10-05).
-- users.folder_name is minted by folder_key.folder_key and used verbatim as
-- users/{folder_name}/... by the upload route. A value outside that alphabet is a
-- folder the upload route cannot write under, so every recording from that person
-- is stranded with no directory row (Deandre, 2026-10-05: `Deandre'_Alberts`).
-- Audited 2026-10-05: prod 32/32 and TEST 17/17 members comply. If a row does not,
-- this statement FAILS and the deploy stops: a bad folder has to be fixed by a
-- person, never silently rewritten (that would orphan its S3 history).
ALTER TABLE users
    ADD CONSTRAINT users_folder_name_charset
    CHECK (folder_name IS NULL OR folder_name ~ '^[A-Za-z0-9._-]+$');
