-- 0089: phone sessions, late-chunk reopen, voice notes (M2 backend).
-- spec: fieldsight-mobile docs/superpowers/specs/2026-10-12-fieldsight-mobile-pwa-design.md §8
--
-- recordings.source (§8 "recordings.source"): which capture path produced the
--   recording -- 'device' (GrandTime body-cam), 'phone' (the mobile PWA) or
--   'web' (the website's upload). Nullable: device_id alone cannot tell a phone
--   from a manual upload, so only rows with a device_id are backfilled to
--   'device'; the rest stay NULL (unknown, not guessed). Per recording, not per
--   session, since one meeting can later hold both.
--
-- meeting_session.session_type (§8 "Voice-note sessions"): 'meeting' or
--   'voice_note', sent by /sessions/{sid}/open. NULL on every existing row and
--   on any open that does not say; a voice note sends no stop email.
--
-- meeting_session.last_chunk_at / closed_chunk_at / reopen_count (§8 "Late
--   chunks re-finalize"): last_chunk_at is the newest chunk's file-name time
--   (the device's own clock, UTC); an idle close snapshots it into
--   closed_chunk_at. The sweep reopens a sent/failed idle-closed row when
--   last_chunk_at > closed_chunk_at -- comparing the device clock with itself,
--   so clock skew cannot cause a reopen -- and bumps reopen_count (capped by
--   REOPEN_MAX in code). idx_meeting_session_reopen serves that scan.
--
-- voice_note_photo (§8 "Voice-note sessions"): photos pinned to a voice note,
--   keyed (user_id, photo_client_uuid) like uq_recordings_client_uuid, because
--   the photo may not be uploaded yet when the note's /open lands. Upsert on the
--   photo, so the latest note wins; the caller's user_id in the key means nobody
--   can claim another user's photos.
--
-- No topic_photos change: topic_photos.source has no CHECK constraint, so the
-- new 'voice_note' value needs nothing here.

ALTER TABLE recordings ADD COLUMN IF NOT EXISTS source text;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'recordings_source_check'
  ) THEN
    ALTER TABLE recordings
      ADD CONSTRAINT recordings_source_check
      CHECK (source IN ('device','phone','web'));
  END IF;
END $$;

UPDATE recordings SET source='device' WHERE device_id IS NOT NULL AND source IS NULL;

ALTER TABLE meeting_session ADD COLUMN IF NOT EXISTS session_type text;
ALTER TABLE meeting_session ADD COLUMN IF NOT EXISTS last_chunk_at timestamptz;
ALTER TABLE meeting_session ADD COLUMN IF NOT EXISTS closed_chunk_at timestamptz;
ALTER TABLE meeting_session ADD COLUMN IF NOT EXISTS reopen_count integer NOT NULL DEFAULT 0;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'meeting_session_session_type_check'
  ) THEN
    ALTER TABLE meeting_session
      ADD CONSTRAINT meeting_session_session_type_check
      CHECK (session_type IN ('meeting','voice_note'));
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_meeting_session_reopen
  ON meeting_session (status)
  WHERE close_intent = 'idle' AND status IN ('sent','failed');

CREATE TABLE IF NOT EXISTS voice_note_photo (
  user_id           uuid NOT NULL REFERENCES users(id),
  photo_client_uuid text NOT NULL,
  session_id        text NOT NULL REFERENCES meeting_session(session_id) ON DELETE CASCADE,
  linked_at         timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, photo_client_uuid)
);

CREATE INDEX IF NOT EXISTS idx_voice_note_photo_session
  ON voice_note_photo (session_id);
