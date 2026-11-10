-- Nightly re-match agent (spec 2026-11-10-nightly-rematch-agent-design.md §3, §5).
-- Every statement is IF NOT EXISTS: the file is safe to execute again.

-- J1: one row per re-emitted request. The unique key is the "never twice for one (N, L)"
-- invariant. A dropped-request re-emit has no L (l_extraction_key NULL); NULLs are distinct
-- under the table UNIQUE, so those rows get their own partial unique index per night
-- (NULLS NOT DISTINCT is not used: it depends on the Aurora PG version).
CREATE TABLE IF NOT EXISTS nightly_reemits (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  site_id           uuid NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
  line              text NOT NULL CHECK (line IN ('xday','reconcile')),
  n_extraction_key  text NOT NULL,
  l_extraction_key  text,
  artifact_key      text NOT NULL,
  night             date NOT NULL,
  created_at        timestamptz DEFAULT now(),
  UNIQUE (line, n_extraction_key, l_extraction_key)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_nightly_reemits_dropped
  ON nightly_reemits (line, n_extraction_key, night) WHERE l_extraction_key IS NULL;

-- J3: a session whose voiceprint-writer claim recompute timed out or failed, so the nightly
-- scan finds it without parsing logs. session_base is the normalised key recompute_session
-- receives. resolved_at NULL = still to do; a new failure reopens a resolved row.
CREATE TABLE IF NOT EXISTS claim_recompute_failures (
  company_id    uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  session_base  text NOT NULL,
  reason        text NOT NULL CHECK (reason IN ('timed_out','failed')),
  failed_at     timestamptz DEFAULT now(),
  resolved_at   timestamptz,
  PRIMARY KEY (company_id, session_base)
);

-- J2: one live nightly link per source row. Four partial unique indexes, one per source-row
-- FK column, instead of one NULLS NOT DISTINCT index. Lexical and other links are untouched.
CREATE UNIQUE INDEX IF NOT EXISTS uq_event_links_nightly_ai ON event_links (src_action_item_id)
  WHERE method LIKE 'nightly_%' AND src_action_item_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_event_links_nightly_fi ON event_links (src_finding_id)
  WHERE method LIKE 'nightly_%' AND src_finding_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_event_links_nightly_de ON event_links (src_decision_id)
  WHERE method LIKE 'nightly_%' AND src_decision_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_event_links_nightly_qu ON event_links (src_question_id)
  WHERE method LIKE 'nightly_%' AND src_question_id IS NOT NULL;
