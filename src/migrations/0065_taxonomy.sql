-- A taxonomy a company can extend, and the rows that hang things on it.
--
-- Nothing tags anything yet. This migration lands the store and the base set
-- only, so the tagging batch is a change to one writer rather than a change to
-- the schema, the writer, the API and the UI at once.
--
-- WHY NOT topics.category
-- -----------------------
-- It already exists and it stays: `category` is load-bearing for Today's
-- Urgent card and for the safety KPI (repositories/topics.py). It is also
-- nearly empty of information. Measured on prod 2026-09-23 over all 516
-- topics: progress 384, quality 85, safety 47 -- 74% of every topic carries
-- the same value. One flat enum, chosen by the extraction prompt from three
-- options, cannot say "architecture / walls" and "commercial / variation"
-- about the same conversation. Tags are ADDITIVE; nothing reads `category`
-- differently after this.

-- ---------------------------------------------------------------------------
-- tag: the vocabulary, two levels, three scopes
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tag (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  -- NULL = the global base set we ship. A customer can never write these.
  company_id  uuid REFERENCES companies(id) ON DELETE CASCADE,
  -- NULL = company-wide. Set = a child a project added for itself.
  site_id     uuid REFERENCES sites(id) ON DELETE CASCADE,
  -- NULL = level 1. RESTRICT, not CASCADE: deleting a parent out from under
  -- its children would silently drop a company's whole sub-vocabulary, and
  -- deactivation (is_active) is what this product does instead of deleting.
  parent_id   uuid REFERENCES tag(id) ON DELETE RESTRICT,
  -- The stable machine key ('architecture.walls'). Labels get renamed; this
  -- does not, because it is what an assignment row and a prompt both name.
  slug        text NOT NULL,
  label       text NOT NULL,
  is_active   boolean NOT NULL DEFAULT true,
  sort_order  int NOT NULL DEFAULT 0,
  created_at  timestamptz NOT NULL DEFAULT now(),
  created_by  uuid REFERENCES users(id)
);

-- THREE PARTIAL UNIQUE INDEXES, not one expression index over COALESCE().
--
-- The COALESCE form needs `ON CONFLICT (COALESCE(company_id, '000...'::uuid), ...)`
-- to match the index expression character for character to infer, which is a
-- footgun for every future writer. Three partial indexes each say exactly one
-- thing, and a bare `ON CONFLICT DO NOTHING` works against all of them.
CREATE UNIQUE INDEX IF NOT EXISTS ux_tag_global_slug ON tag (slug)
  WHERE company_id IS NULL AND site_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_tag_company_slug ON tag (company_id, slug)
  WHERE company_id IS NOT NULL AND site_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_tag_site_slug ON tag (site_id, slug)
  WHERE site_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_tag_company ON tag (company_id) WHERE company_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_tag_parent ON tag (parent_id) WHERE parent_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- tag_run: which batch wrote which tags, so a batch can be undone
-- ---------------------------------------------------------------------------
--
-- Re-tagging after a taxonomy change has to be reversible as a BATCH. Without
-- a run id the only way to undo one is to delete by (source, time window),
-- which cannot tell a bad run's rows from a good one that overlapped it.
CREATE TABLE IF NOT EXISTS tag_run (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id       uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  taxonomy_version int NOT NULL,
  method           text NOT NULL,     -- 'extraction' | 'classifier' | 'embedding'
  status           text NOT NULL DEFAULT 'running',  -- 'running'|'done'|'rolled_back'
  stats            jsonb NOT NULL DEFAULT '{}'::jsonb,
  started_at       timestamptz NOT NULL DEFAULT now(),
  finished_at      timestamptz,
  created_by       uuid REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_tag_run_company ON tag_run (company_id, started_at DESC);

-- ---------------------------------------------------------------------------
-- The assignments. Two tables, not one polymorphic one.
-- ---------------------------------------------------------------------------
--
-- A single (target_type, target_id) table cannot carry a foreign key, so a
-- deleted topic leaves its tag rows behind forever and every reader has to
-- remember to join them away. Two tables get ON DELETE CASCADE for free, which
-- is how every other child of `topics` in this schema already behaves.
--
-- `source` is the same vocabulary the photo binding learned the hard way:
-- a machine may replace what a machine wrote, and never what a person chose.
CREATE TABLE IF NOT EXISTS topic_tags (
  topic_id   uuid NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
  tag_id     uuid NOT NULL REFERENCES tag(id) ON DELETE CASCADE,
  source     text NOT NULL,          -- 'extraction'|'classifier'|'embedding'|'human'
  confidence real,
  run_id     uuid REFERENCES tag_run(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (topic_id, tag_id)
);
CREATE INDEX IF NOT EXISTS idx_topic_tags_tag ON topic_tags (tag_id);
CREATE INDEX IF NOT EXISTS idx_topic_tags_run ON topic_tags (run_id) WHERE run_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS action_item_tags (
  action_item_id uuid NOT NULL REFERENCES action_items(id) ON DELETE CASCADE,
  tag_id         uuid NOT NULL REFERENCES tag(id) ON DELETE CASCADE,
  source         text NOT NULL,
  confidence     real,
  run_id         uuid REFERENCES tag_run(id) ON DELETE SET NULL,
  created_at     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (action_item_id, tag_id)
);
CREATE INDEX IF NOT EXISTS idx_action_item_tags_tag ON action_item_tags (tag_id);
CREATE INDEX IF NOT EXISTS idx_action_item_tags_run ON action_item_tags (run_id) WHERE run_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- The global base set: 12 level-1, 70 level-2.
-- ---------------------------------------------------------------------------
--
-- Seeded HERE rather than by a script, because it is reference data with fixed
-- slugs that every environment must have before anything can read a tag, and
-- a script that has to be remembered is a script that is not run on the
-- environment nobody was looking at.
--
-- Counted against a real database rather than by eye: applying this file inside
-- a rollback-only transaction on TEST yields 12 parents, 70 children, 82 rows,
-- every child's slug prefixed by its parent's, and no third level. The first
-- draft of this comment said 65, which is what counting in your head gets you.
--
-- Idempotent: `ON CONFLICT DO NOTHING` against the partial unique index above,
-- so a re-run adds nothing. A label EDIT made later by us would not be picked
-- up by a re-run -- that is deliberate, since a company may have renamed it,
-- and shadowing (a company row with the same slug) is how an override is meant
-- to happen.
--
-- Chosen to cover the dimensions a construction site actually talks about
-- rather than to match any one trade's vocabulary. Deliberately NOT included:
-- anything that duplicates a column that already exists (work/non-work,
-- inspection/meeting origin, severity), and anything that is a person or a
-- place -- those are `participants` and the location markers.
INSERT INTO tag (company_id, site_id, parent_id, slug, label, sort_order)
SELECT NULL, NULL, NULL, v.slug, v.label, v.ord
  FROM (VALUES
    ('structure',    'Structure',                10),
    ('architecture', 'Architecture',             20),
    ('envelope',     'Envelope & weathertightness', 30),
    ('mechanical',   'Mechanical',               40),
    ('electrical',   'Electrical',               50),
    ('finishes',     'Finishes',                 60),
    ('sitework',     'Sitework & civil',         70),
    ('quality',      'Quality & compliance',     80),
    ('safety',       'Safety',                   90),
    ('commercial',   'Commercial',              100),
    ('programme',    'Programme & logistics',   110),
    ('stakeholders', 'Stakeholders',            120)
  ) AS v(slug, label, ord)
ON CONFLICT DO NOTHING;

INSERT INTO tag (company_id, site_id, parent_id, slug, label, sort_order)
SELECT NULL, NULL, p.id, v.slug, v.label, v.ord
  FROM (VALUES
    ('structure',    'structure.foundations',            'Foundations',            10),
    ('structure',    'structure.slab',                   'Slab',                   20),
    ('structure',    'structure.framing',                'Framing',                30),
    ('structure',    'structure.steelwork',              'Steelwork',              40),
    ('structure',    'structure.concrete',               'Concrete',               50),
    ('structure',    'structure.precast',                'Precast',                60),

    ('architecture', 'architecture.walls',               'Walls',                  10),
    ('architecture', 'architecture.ceilings',            'Ceilings',               20),
    ('architecture', 'architecture.floorings',           'Floorings',              30),
    ('architecture', 'architecture.roofing',             'Roofing',                40),
    ('architecture', 'architecture.windows-and-doors',   'Windows & doors',        50),
    ('architecture', 'architecture.joinery',             'Joinery',                60),
    ('architecture', 'architecture.cladding',            'Cladding',               70),
    ('architecture', 'architecture.stairs',              'Stairs & balustrades',   80),

    ('envelope',     'envelope.waterproofing',           'Waterproofing',          10),
    ('envelope',     'envelope.insulation',              'Insulation',             20),
    ('envelope',     'envelope.air-barrier',             'Air barrier',            30),
    ('envelope',     'envelope.glazing-seals',           'Glazing & seals',        40),
    ('envelope',     'envelope.external-drainage',       'External drainage',      50),

    ('mechanical',   'mechanical.hvac',                  'HVAC',                   10),
    ('mechanical',   'mechanical.ductwork',              'Ductwork',               20),
    ('mechanical',   'mechanical.plumbing',              'Plumbing',               30),
    ('mechanical',   'mechanical.drainage',              'Drainage',               40),
    ('mechanical',   'mechanical.fire-services',         'Fire services',          50),
    ('mechanical',   'mechanical.lifts',                 'Lifts',                  60),

    ('electrical',   'electrical.power',                 'Power',                  10),
    ('electrical',   'electrical.lighting',              'Lighting',               20),
    ('electrical',   'electrical.data-comms',            'Data & comms',           30),
    ('electrical',   'electrical.switchboard',           'Switchboard',            40),
    ('electrical',   'electrical.security-and-access',   'Security & access',      50),

    ('finishes',     'finishes.plastering',              'Plastering',             10),
    ('finishes',     'finishes.painting',                'Painting',               20),
    ('finishes',     'finishes.tiling',                  'Tiling',                 30),
    ('finishes',     'finishes.floor-finish',            'Floor finish',           40),
    ('finishes',     'finishes.fixtures-and-fittings',   'Fixtures & fittings',    50),

    ('sitework',     'sitework.earthworks',              'Earthworks',             10),
    ('sitework',     'sitework.excavation',              'Excavation',             20),
    ('sitework',     'sitework.services-trenching',      'Services trenching',     30),
    ('sitework',     'sitework.paving',                  'Paving',                 40),
    ('sitework',     'sitework.landscaping',             'Landscaping',            50),
    ('sitework',     'sitework.temporary-works',         'Temporary works',        60),

    ('quality',      'quality.defect',                   'Defect',                 10),
    ('quality',      'quality.rework',                   'Rework',                 20),
    ('quality',      'quality.inspection',               'Inspection',             30),
    ('quality',      'quality.test-and-commissioning',   'Test & commissioning',   40),
    ('quality',      'quality.consent-and-code',         'Consent & code',         50),
    ('quality',      'quality.as-built',                 'As-built',               60),

    ('safety',       'safety.hazard',                    'Hazard',                 10),
    ('safety',       'safety.incident-or-near-miss',     'Incident / near miss',   20),
    ('safety',       'safety.ppe',                       'PPE',                    30),
    ('safety',       'safety.permit-and-isolation',      'Permit & isolation',     40),
    ('safety',       'safety.traffic-management',        'Traffic management',     50),
    ('safety',       'safety.induction',                 'Induction',              60),

    ('commercial',   'commercial.variation',             'Variation',              10),
    ('commercial',   'commercial.cost-and-pricing',      'Cost & pricing',         20),
    ('commercial',   'commercial.claim-and-payment',     'Claim & payment',        30),
    ('commercial',   'commercial.contract-and-scope',    'Contract & scope',       40),
    ('commercial',   'commercial.procurement',           'Procurement',            50),

    ('programme',    'programme.schedule',               'Schedule',               10),
    ('programme',    'programme.delay-and-disruption',   'Delay & disruption',     20),
    ('programme',    'programme.delivery-and-materials', 'Delivery & materials',   30),
    ('programme',    'programme.plant-and-equipment',    'Plant & equipment',      40),
    ('programme',    'programme.access-and-sequencing',  'Access & sequencing',    50),
    ('programme',    'programme.labour',                 'Labour',                 60),

    ('stakeholders', 'stakeholders.client',              'Client',                 10),
    ('stakeholders', 'stakeholders.consultant',          'Consultant & designer',  20),
    ('stakeholders', 'stakeholders.subcontractor',       'Subcontractor',          30),
    ('stakeholders', 'stakeholders.supplier',            'Supplier',               40),
    ('stakeholders', 'stakeholders.council',             'Council & inspector',    50),
    ('stakeholders', 'stakeholders.neighbour',           'Neighbour & public',     60)
  ) AS v(parent_slug, slug, label, ord)
  JOIN tag p ON p.slug = v.parent_slug AND p.company_id IS NULL AND p.site_id IS NULL
ON CONFLICT DO NOTHING;
