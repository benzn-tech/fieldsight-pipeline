-- 0095: action packs -- the background action agent's stored runs
-- (spec docs/superpowers/specs/2026-11-01-background-action-agent-design.md §3 + §14).
--
-- action_packs         one run: the instruction, the run's reading, its conclusion, run state.
-- action_pack_sources  one row per cited ref (R3, D2, W5), so a pack is self-contained.
-- action_pack_actions  the proposed actions, each applied/dismissed one at a time by a person.
--
-- Tenancy (§14 D4): a pack covers ONE site and belongs to that site's company. company_id is
-- written from sites.company_id by repositories.action_packs.create_queued, never from the
-- caller. Packs are an archive (G5): nothing deletes them (archived_at hides them), so the
-- FKs to companies/sites/users are plain (no CASCADE -- a CASCADE would silently destroy the
-- archive, and nothing in the code deletes a site, company or user). Sources and actions
-- belong to their pack and cascade with it.
--
-- state:   queued -> running -> ready | failed | cancelled (§4, §14 S1).
-- failure: a reason code, set exactly when state = 'failed' (§4.5; company_inactive §7).
-- heartbeat_at (§14 S9): last liveness stamp -- NOT NULL DEFAULT now() (so a queued pack is
--   always sweepable), re-set at claim, refreshed by the
--   worker after each round. The stale-run sweep fails queued/running packs whose stamp is
--   older than the threshold; action_packs_sweep serves it.
-- One active pack per creator: the partial unique index action_packs_one_active_per_creator
--   (create_queued maps its violation to ActivePackExists -> 409).
--
-- Additive only: no existing code path reads these tables. Idempotent (IF NOT EXISTS).
-- No GRANT (single DB role).
CREATE TABLE IF NOT EXISTS action_packs (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id    uuid NOT NULL REFERENCES companies(id),
  site_id       uuid NOT NULL REFERENCES sites(id),
  created_by    uuid NOT NULL REFERENCES users(id),
  title         text,
  instruction   text NOT NULL,
  reading       text,
  conclusion    jsonb,
  state         text NOT NULL DEFAULT 'queued'
                CHECK (state IN ('queued','running','ready','failed','cancelled')),
  failure       text
                CHECK (failure IN ('budget','model','tools','verifier','internal',
                                   'company_inactive')),
  heartbeat_at  timestamptz NOT NULL DEFAULT now(),
  run_trace     jsonb,
  models        jsonb,
  as_of         timestamptz,
  archived_at   timestamptz,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT action_packs_failure_iff_failed CHECK ((state = 'failed') = (failure IS NOT NULL))
);

CREATE UNIQUE INDEX IF NOT EXISTS action_packs_one_active_per_creator
  ON action_packs (created_by) WHERE state IN ('queued','running');
CREATE INDEX IF NOT EXISTS action_packs_company_created
  ON action_packs (company_id, created_at);
CREATE INDEX IF NOT EXISTS action_packs_creator_state
  ON action_packs (created_by, state);
-- pm/site_manager reads ("packs about my sites").
CREATE INDEX IF NOT EXISTS action_packs_site_created
  ON action_packs (site_id, created_at);
CREATE INDEX IF NOT EXISTS action_packs_sweep
  ON action_packs (state, heartbeat_at) WHERE state IN ('queued','running');

-- kind/provenance (§3.2): web kinds come only from the web lane and the web lane gives only
-- web kinds; records/documents are not pinned further (a chunk may be either).
CREATE TABLE IF NOT EXISTS action_pack_sources (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  pack_id         uuid NOT NULL REFERENCES action_packs(id) ON DELETE CASCADE,
  ref             text NOT NULL,
  kind            text NOT NULL
                  CHECK (kind IN ('topic','chunk','document','web_answer','web_source','item')),
  provenance      text NOT NULL CHECK (provenance IN ('records','documents','web')),
  topic_id        uuid,
  chunk_id        uuid,
  document_id     uuid,
  revision        integer,
  page_from       integer,
  page_to         integer,
  stable_id       uuid,
  title           text,
  date            date,
  site_id         uuid,
  who             text,
  excerpt         text,
  url             text,
  fetched_at      timestamptz,
  snapshot_s3_key text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT action_pack_sources_web_kind_iff_web
    CHECK ((provenance = 'web') = (kind IN ('web_answer','web_source'))),
  CONSTRAINT action_pack_sources_pack_ref_key UNIQUE (pack_id, ref)
);

-- state (§3.3): proposed -> applied | dismissed | stale | failed. Transitions are enforced by
-- repositories.action_packs.set_action_state (only from proposed).
CREATE TABLE IF NOT EXISTS action_pack_actions (
  id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  pack_id                uuid NOT NULL REFERENCES action_packs(id) ON DELETE CASCADE,
  seq                    integer NOT NULL,
  type                   text NOT NULL
                         CHECK (type IN ('close_item','update_item','create_item','draft_rfi',
                                         'draft_email','note')),
  target_stable_id       uuid,
  target_snapshot        jsonb,
  proposed               jsonb,
  rationale              text,
  refs                   text[] NOT NULL CHECK (cardinality(refs) >= 1),
  verifier               text NOT NULL CHECK (verifier IN ('supported','weak','unsupported')),
  state                  text NOT NULL DEFAULT 'proposed'
                         CHECK (state IN ('proposed','applied','dismissed','stale','failed')),
  applied_by             uuid REFERENCES users(id),
  applied_at             timestamptz,
  applied_result         jsonb,
  created_item_stable_id uuid,
  created_at             timestamptz NOT NULL DEFAULT now(),
  updated_at             timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT action_pack_actions_target_required
    CHECK (type NOT IN ('close_item','update_item') OR target_stable_id IS NOT NULL),
  CONSTRAINT action_pack_actions_pack_seq_key UNIQUE (pack_id, seq)
);
