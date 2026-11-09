-- 0099: site decision loop P0 (shadow) -- the event hub, its runs, decision cards and the
-- decision ledger (spec docs/superpowers/specs/2026-11-05-site-decision-loop-design.md §5).
--
-- site_events         one row per event a source emits for a site; seq is the replay order.
-- event_runs          one handler attempt; run_events joins it to every event it handled.
-- decision_scan_seen  checklist-scan cursor: a report is marked seen only once its result
--                     read 'done'.
-- decision_cards      the question put to people (P0: shadow, status stays 'open', labelled).
-- decision_ledger     what was decided, by whom, on what authority. APPEND-ONLY.
--
-- The stack has a single DB role (see 0092/0093/0095 "No GRANT"), so a REVOKE cannot make the
-- ledger append-only: it is append-only by trigger (decision_ledger_append_only rejects every
-- UPDATE and DELETE; decision_ledger_no_truncate rejects TRUNCATE). Changing one's mind is
-- a new row with supersedes_entry_id.
--
-- Dedupe: one site_events row per dedupe_key among rows that are not 'duplicate' (partial
-- unique index); one OPEN card per card_key (partial unique index), so a superseded or
-- answered card frees the key.
--
-- Tenancy: site_events, event_runs, run_events, decision_cards and decision_ledger carry
-- company_id + site_id (NOT NULL); repositories write company_id from sites.company_id,
-- never from the caller. decision_scan_seen is the one exemption: an internal scan cursor
-- keyed on checklist_reports.id, never read by an API, holding no customer content, and a
-- report can resolve to 0..n sites, so it has no single site to carry. FKs to companies/sites/users are plain (no
-- CASCADE), as in 0095: the ledger is an archive, and a CASCADE into it would be rejected by
-- the trigger anyway. Rows that belong to a parent (run_events) cascade with it.
--
-- Additive only: no existing code path reads these tables. Idempotent (IF NOT EXISTS,
-- CREATE OR REPLACE FUNCTION, DROP TRIGGER IF EXISTS). No GRANT (single DB role).

CREATE TABLE IF NOT EXISTS site_events (
  event_id     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  seq          bigserial UNIQUE,
  company_id   uuid NOT NULL REFERENCES companies(id),
  site_id      uuid NOT NULL REFERENCES sites(id),
  source       text NOT NULL,
  type         text NOT NULL,
  subject_ref  text NOT NULL,
  bucket       text NOT NULL DEFAULT '',
  occurred_at  timestamptz,
  received_at  timestamptz NOT NULL DEFAULT now(),
  summary      text,
  link         text,
  payload      jsonb,
  chain_id     uuid,
  chain_depth  int NOT NULL DEFAULT 0,
  dedupe_key   text NOT NULL,
  status       text NOT NULL
               CHECK (status IN ('received','late','unmatched','duplicate','merged','queued',
                                 'running','done','nothing','failed','blocked','too_deep')),
  rule_id      text,
  attempts     int NOT NULL DEFAULT 0,
  not_before   timestamptz,
  last_error   text
);
CREATE UNIQUE INDEX IF NOT EXISTS site_events_dedupe_key_live
  ON site_events (dedupe_key) WHERE status <> 'duplicate';
CREATE INDEX IF NOT EXISTS idx_site_events_status_not_before
  ON site_events (status, not_before);
CREATE INDEX IF NOT EXISTS idx_site_events_subject
  ON site_events (company_id, site_id, type, subject_ref);

CREATE TABLE IF NOT EXISTS event_runs (
  run_id       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id   uuid NOT NULL REFERENCES companies(id),
  site_id      uuid NOT NULL REFERENCES sites(id),
  attempt      int NOT NULL DEFAULT 1,
  handler      text NOT NULL,
  claim_id     uuid,
  lease_until  timestamptz,
  started_at   timestamptz,
  ended_at     timestamptz,
  outcome      text CHECK (outcome IN ('done','nothing','failed','unknown','cancelled')),
  result_hash  text,
  error        text,
  tokens_in    int,
  tokens_out   int,
  cost_usd     numeric,
  model_calls  int NOT NULL DEFAULT 0,
  models       jsonb
);

CREATE TABLE IF NOT EXISTS run_events (
  run_id      uuid NOT NULL REFERENCES event_runs(run_id) ON DELETE CASCADE,
  event_id    uuid NOT NULL REFERENCES site_events(event_id) ON DELETE CASCADE,
  company_id  uuid NOT NULL REFERENCES companies(id),
  site_id     uuid NOT NULL REFERENCES sites(id),
  PRIMARY KEY (run_id, event_id)
);
CREATE INDEX IF NOT EXISTS idx_run_events_event ON run_events (event_id);

-- report_id is a checklist_reports id; no FK: this is a scan cursor, and a deleted report
-- leaving a stale marker is harmless (it is never scanned again either way).
CREATE TABLE IF NOT EXISTS decision_scan_seen (
  report_id   uuid NOT NULL,
  result_key  text NOT NULL,
  seen_at     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (report_id, result_key)
);

CREATE TABLE IF NOT EXISTS decision_cards (
  card_id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  event_id         uuid REFERENCES site_events(event_id),
  company_id       uuid NOT NULL REFERENCES companies(id),
  site_id          uuid NOT NULL REFERENCES sites(id),
  rule_id          text NOT NULL,
  subject_ref      text NOT NULL,
  purpose          text NOT NULL,
  card_key         text NOT NULL,
  kind             text NOT NULL CHECK (kind IN ('approve','choose','fact','fault','rule')),
  question         text NOT NULL,
  consequence      text,
  options          jsonb NOT NULL DEFAULT '[]'::jsonb,
  evidence         jsonb NOT NULL DEFAULT '[]'::jsonb,
  urgency          text,
  deadline_at      timestamptz,
  timeout_default  text,
  recipients       jsonb NOT NULL DEFAULT '[]'::jsonb,
  version          int NOT NULL DEFAULT 1,
  delivered_refs   jsonb NOT NULL DEFAULT '[]'::jsonb,
  status           text NOT NULL DEFAULT 'open'
                   CHECK (status IN ('open','answered','done','timed_out','superseded')),
  shadow_label     text CHECK (shadow_label IN ('needed','not_needed','wrong_evidence')),
  labelled_by      uuid REFERENCES users(id),
  labelled_at      timestamptz,
  label_note       text,
  created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS decision_cards_one_open_per_key
  ON decision_cards (card_key) WHERE status = 'open';
CREATE INDEX IF NOT EXISTS idx_decision_cards_site_status
  ON decision_cards (company_id, site_id, status);
CREATE INDEX IF NOT EXISTS idx_decision_cards_event ON decision_cards (event_id);

-- decided_by is NULL for source 'timeout_default' (nobody decided; the default applied).
CREATE TABLE IF NOT EXISTS decision_ledger (
  entry_id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id           uuid NOT NULL REFERENCES companies(id),
  site_id              uuid NOT NULL REFERENCES sites(id),
  card_id              uuid REFERENCES decision_cards(card_id),
  subject_ref          text NOT NULL,
  decision             text NOT NULL,
  kind                 text NOT NULL CHECK (kind IN ('decided','declined','defaulted')),
  decided_by           uuid REFERENCES users(id),
  authority_basis      text,
  decided_at           timestamptz NOT NULL DEFAULT now(),
  evidence             jsonb NOT NULL DEFAULT '[]'::jsonb,
  supersedes_entry_id  uuid REFERENCES decision_ledger(entry_id),
  source               text NOT NULL CHECK (source IN ('card','timeout_default','meeting'))
);
CREATE INDEX IF NOT EXISTS idx_decision_ledger_subject
  ON decision_ledger (company_id, site_id, subject_ref);

CREATE OR REPLACE FUNCTION decision_ledger_reject_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'decision_ledger is append-only';
END;
$$;

DROP TRIGGER IF EXISTS decision_ledger_append_only ON decision_ledger;
CREATE TRIGGER decision_ledger_append_only
  BEFORE UPDATE OR DELETE ON decision_ledger
  FOR EACH ROW EXECUTE FUNCTION decision_ledger_reject_change();

-- TRUNCATE skips row triggers, so it needs its own statement-level guard.
DROP TRIGGER IF EXISTS decision_ledger_no_truncate ON decision_ledger;
CREATE TRIGGER decision_ledger_no_truncate
  BEFORE TRUNCATE ON decision_ledger
  FOR EACH STATEMENT EXECUTE FUNCTION decision_ledger_reject_change();
