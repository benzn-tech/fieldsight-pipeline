-- 0027: programme tables. Replaces the single S3 programmes/{site_id}/
-- programme.json document (src/repositories/programme.py) as the source of
-- truth. Spec: fieldsight-ui/docs/superpowers/specs/
-- 2026-08-02-programme-foundation-design.md §4.
--
-- Two identifiers per task, deliberately separate:
--   id             surrogate UUID. IDENTITY. Every foreign key points here.
--   source_task_id whatever the imported file calls it. MATCHING only.
-- Reconciliation joins on source_task_id; nothing else may. A planner
-- renaming a P6 Activity ID must cost one column update, not a cascade of
-- broken references. See design §4.1.

CREATE TABLE programmes (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  site_id          uuid NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
  name             text NOT NULL,
  source_format    text,
  current_version  int  NOT NULL DEFAULT 0,
  baseline_version int,
  is_primary       boolean NOT NULL DEFAULT true,
  status           text NOT NULL DEFAULT 'active'
                   CHECK (status IN ('active','archived')),
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now()
);
-- A site may hold several programmes (main contract, subcontractor, option
-- study) but exactly one drives Today / My Work rollups.
CREATE UNIQUE INDEX uq_programmes_primary ON programmes (site_id)
  WHERE status = 'active' AND is_primary;
CREATE INDEX idx_programmes_site ON programmes (site_id, status);

CREATE TABLE programme_versions (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  programme_id  uuid NOT NULL REFERENCES programmes(id) ON DELETE CASCADE,
  version_no    int  NOT NULL,
  filename      text,
  mode          text NOT NULL CHECK (mode IN ('initial','update','replace')),
  imported_by   uuid REFERENCES users(id),
  imported_at   timestamptz NOT NULL DEFAULT now(),
  diff_summary  jsonb NOT NULL DEFAULT '{}'::jsonb,
  UNIQUE (programme_id, version_no)
);

CREATE TABLE programme_tasks (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  programme_id       uuid NOT NULL REFERENCES programmes(id) ON DELETE CASCADE,
  source_task_id     text,
  parent_id          uuid REFERENCES programme_tasks(id) ON DELETE CASCADE,

  origin             text NOT NULL CHECK (origin IN ('imported','local')),
  name               text NOT NULL,
  wbs_code           text,
  start_date         date,
  end_date           date,
  duration_days      int,
  progress_pct       smallint NOT NULL DEFAULT 0
                     CHECK (progress_pct BETWEEN 0 AND 100),
  status             text NOT NULL DEFAULT 'not_started',
  zone               text,

  total_float_days   int,
  is_critical        boolean NOT NULL DEFAULT false,

  first_seen_version int NOT NULL DEFAULT 1,
  removed_in_version int,
  locally_modified   boolean NOT NULL DEFAULT false,

  sort_order         int NOT NULL DEFAULT 0,
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now(),
  updated_by         uuid REFERENCES users(id),
  row_version        int NOT NULL DEFAULT 1,

  -- A local row has no file identity; an imported row must have one, or
  -- reconciliation cannot match it and it would silently duplicate.
  CHECK ((origin = 'local'    AND source_task_id IS NULL)
      OR (origin = 'imported' AND source_task_id IS NOT NULL))
);
-- The reconciliation key. Partial so the many local rows (all NULL) do not
-- collide with each other.
CREATE UNIQUE INDEX uq_ptasks_source ON programme_tasks (programme_id, source_task_id)
  WHERE source_task_id IS NOT NULL;
CREATE INDEX idx_ptasks_window ON programme_tasks (programme_id, start_date, end_date)
  WHERE removed_in_version IS NULL;
CREATE INDEX idx_ptasks_parent ON programme_tasks (parent_id);

CREATE TABLE programme_task_deps (
  predecessor_id uuid NOT NULL REFERENCES programme_tasks(id) ON DELETE CASCADE,
  successor_id   uuid NOT NULL REFERENCES programme_tasks(id) ON DELETE CASCADE,
  dep_type       text NOT NULL DEFAULT 'FS'
                 CHECK (dep_type IN ('FS','SS','FF','SF')),
  lag_days       int  NOT NULL DEFAULT 0,
  PRIMARY KEY (predecessor_id, successor_id, dep_type),
  CHECK (predecessor_id <> successor_id)
);
CREATE INDEX idx_pdeps_successor ON programme_task_deps (successor_id);

CREATE TABLE programme_task_assignees (
  task_id   uuid NOT NULL REFERENCES programme_tasks(id) ON DELETE CASCADE,
  -- folder_name, matching the assignee axis Today and Tasks already use.
  assignee  text NOT NULL,
  role      text NOT NULL DEFAULT 'owner' CHECK (role IN ('owner','contributor')),
  PRIMARY KEY (task_id, assignee)
);
CREATE INDEX idx_ptassignees_assignee ON programme_task_assignees (assignee);

-- A site manager cannot move a contract date — the next import would
-- overwrite it anyway. This is how site knowledge reaches the PM instead.
CREATE TABLE programme_delay_flags (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id       uuid NOT NULL REFERENCES programme_tasks(id) ON DELETE CASCADE,
  raised_by     uuid NOT NULL REFERENCES users(id),
  reason        text NOT NULL,
  expected_end  date,
  state         text NOT NULL DEFAULT 'open'
                CHECK (state IN ('open','acknowledged','resolved')),
  created_at    timestamptz NOT NULL DEFAULT now(),
  resolved_at   timestamptz
);
CREATE INDEX idx_pdelay_task_state ON programme_delay_flags (task_id, state);
