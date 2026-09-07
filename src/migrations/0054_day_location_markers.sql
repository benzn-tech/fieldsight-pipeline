-- Where the speaker said he was, as a STATE that persists between announcements.
--
-- A photo used to be attributable only to whatever was being SAID within two
-- minutes of the shutter. For a room inspection that is almost never anything:
-- Neil / 2026-09-02 is 27 seconds of speech across 13 minutes and 53 photos,
-- five announcements ("Photos of level two progress", "...level three
-- progress", ...) each followed by silence. 37% of topic time windows in this
-- database are a single instant.
--
-- Markers are NOT topics and must not be stored as them. A topic is something
-- that was discussed; a marker is where he was standing, and the difference is
-- the whole point: if he meets someone in Room 101 and they talk about a level
-- three delay, the conversation becomes a topic and he is STILL IN ROOM 101.
-- Only another announcement moves him. Storing markers as topics would let a
-- conversation silently relocate him.
--
-- One row per (company, folder, day) holding the day's markers in order, rather
-- than a row per marker: they are read as a whole (the timeline is derived by
-- looking at each marker's successor) and written as a whole (a re-extraction
-- replaces the day's set). A row per marker would need its own delete-and-
-- reinsert dance on every re-extraction to avoid duplicates.
--
-- markers: [{"at": "HH:MM", "location": "...", "quote": "..."}], oldest first.
-- `quote` is provenance, not decoration -- it is what makes a wrong marker
-- diagnosable instead of mysterious, the same discipline the claim-provenance
-- work applies to extracted facts.
CREATE TABLE IF NOT EXISTS day_location_markers (
    company_id   uuid        NOT NULL,
    user_folder  text        NOT NULL,
    report_date  date        NOT NULL,
    markers      jsonb       NOT NULL DEFAULT '[]'::jsonb,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (company_id, user_folder, report_date)
);

-- The read is always "this folder, this day" and the primary key already serves
-- it. No extra index: an empty table with a redundant index is a cost with no
-- reader.
