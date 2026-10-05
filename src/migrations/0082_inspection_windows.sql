-- A check the speaker said he was doing, and when (voice-triggered checklists,
-- owner 2026-09-30): "starting the pre-pour concrete check" opens a window that
-- closes on "...check done", on the next check, or where the recording stopped.
-- Matched (when it can be) to the company's checklist template of that name, so
-- the report dialog can offer "Pre-pour checklist, 10:59-11:02" in one click.
--
-- One row per check per recording; a recording's rows are replaced whenever it
-- is re-extracted (inspection_windows.replace_for_session). Times are the
-- day's clock to the second, as text ('HH:MM:SS') -- the same shape the
-- location markers carry.
CREATE TABLE IF NOT EXISTS inspection_windows (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id   uuid NOT NULL REFERENCES companies(id),
  user_folder  text NOT NULL,
  report_date  date NOT NULL,
  session      text NOT NULL,
  name         text NOT NULL,
  kind         text NOT NULL DEFAULT '',
  start_at     text NOT NULL,
  end_at       text,
  -- 'said' | 'next_check' | 'recording_stop'
  end_source   text NOT NULL,
  start_quote  text NOT NULL DEFAULT '',
  end_quote    text,
  -- NULL when no checklist template of the company matched what he said.
  template_id  uuid REFERENCES report_templates(id) ON DELETE SET NULL,
  match_score  real,
  created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_inspection_windows_day
  ON inspection_windows (company_id, user_folder, report_date);
CREATE INDEX IF NOT EXISTS idx_inspection_windows_session
  ON inspection_windows (session);
