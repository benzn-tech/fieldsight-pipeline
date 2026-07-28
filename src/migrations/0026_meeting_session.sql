-- 0026: meeting_session — the rolling, device-minted session lifecycle behind the
-- voice-timeliness paradigm (≤2-min confirmation email + mis-touch grace zone).
--
-- session_id is minted ON THE DEVICE (32 hex, hyphen-free) so grouping survives
-- offline recording; it is the durable key every ~1-min chunk / topic / recording
-- of one press-record→stop shares. `version` is the optimistic lock that makes the
-- grace finalize idempotent: a resume within the grace window bumps it, so a
-- stale scheduled finalize no-ops (mis-touch protection).
--
-- spec: docs/superpowers/specs/2026-07-27-asr-switch-stop-continuity-test-design.md §3, §8.4
CREATE TABLE meeting_session (
  session_id      text PRIMARY KEY CHECK (session_id ~ '^[0-9a-f]{32}$'),
  company_id      uuid NOT NULL REFERENCES companies(id),
  user_id         uuid NOT NULL REFERENCES users(id),
  site_id         uuid REFERENCES sites(id),
  kind            text CHECK (kind IN ('audio','video')),
  status          text NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open','pending_close','finalizing','sent','failed')),
  version         integer NOT NULL DEFAULT 0,
  opened_at       timestamptz,
  last_segment_at timestamptz,
  closed_at       timestamptz,
  close_intent    text CHECK (close_intent IN ('end','idle')),
  rolling_summary text,
  rolling_summary_version integer NOT NULL DEFAULT 0,
  segment_count   integer NOT NULL DEFAULT 0,
  word_count      integer NOT NULL DEFAULT 0,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_meeting_session_user_opened ON meeting_session (user_id, opened_at);
CREATE INDEX idx_meeting_session_status ON meeting_session (status);
CREATE INDEX idx_meeting_session_site ON meeting_session (site_id);
