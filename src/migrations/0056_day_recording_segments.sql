-- When a person was recording, as raw segments, so a day can be shown as blocks.
--
-- The report picker needs to offer "10:55-11:35" as one selectable stretch: the
-- stretches are the runs of recorded audio separated by more than ten minutes of
-- nothing. Topics alone cannot do it -- on an intermittent day they over-split one
-- meeting into its agenda items, and they drop audio no topic was extracted from
-- (2026-09-02 has 17:14-17:28 recorded and no topic for it).
--
-- SEGMENTS, not blocks (design review finding F9). The first design stored merged
-- blocks with the thresholds they were computed with, which means changing a
-- threshold never reaches a day that has stopped receiving transcripts. Stored raw,
-- org-api merges at read time with whatever the thresholds are now, and it is the
-- single reader of both -- two functions can never disagree about a block.
--
-- segments: [{"start": s, "end": s, "session_id": "<32hex>"|null, "key": "transcripts/..."}]
-- start/end are seconds since midnight on the DEVICE wall clock -- the same clock
-- the transcript filenames and topics.time_range carry. No timezone conversion.
-- `key` is kept so a deleted recording can be filtered out at read time by the same
-- prefix tombstones every other reader uses (finding F1); nothing here is ever
-- deleted in place.
--
-- source_object_count makes the write monotonic (finding F4): a LIST that saw fewer
-- objects than the stored one is an older, out-of-order view of the same day and must
-- not shrink it. dirty is the debounce's memory -- a transcript that landed while the
-- day had just been computed marks the row, and the five-minute trailing pass
-- recomputes it, so the last transcripts of a day are never lost to the debounce.
--
-- dirty_since (fix for a race found in review round 1, I1): two concurrent computes
-- can both LIST an old day before either writes. If the one that started EARLIER
-- (and so missed a transcript that landed mid-flight) writes LAST, a dirty/count-only
-- rule would let its write clear a mark raised in between, and the late transcript is
-- lost with the row reading clean. dirty_since records when the CURRENT mark was
-- raised (the earliest of any marks since the row was last clean); upsert_monotonic
-- only clears dirty when the write's own `listed_at` (captured before that LIST
-- began) is at or after dirty_since -- i.e. the write's view could have seen whatever
-- caused the mark. A write whose LIST started before the mark leaves dirty/dirty_since
-- untouched, so the day stays flagged for the trailing pass. NULL means "not marked
-- since the last clean write".
--
-- Keyed by user rather than folder because users.folder_name can be changed and the
-- person cannot; folder_name is carried so the trailing pass can LIST without a join.
CREATE TABLE IF NOT EXISTS day_recording_segments (
    user_id             uuid        NOT NULL REFERENCES users(id),
    report_date         date        NOT NULL,
    folder_name         text        NOT NULL,
    segments            jsonb       NOT NULL,
    source_object_count int         NOT NULL,
    dirty               boolean     NOT NULL DEFAULT false,
    dirty_since         timestamptz,
    computed_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, report_date)
);

-- The trailing pass reads "every dirty row, oldest first" every five minutes. Partial,
-- because almost every row is clean almost all of the time.
CREATE INDEX IF NOT EXISTS day_recording_segments_dirty_idx
    ON day_recording_segments (computed_at) WHERE dirty;
