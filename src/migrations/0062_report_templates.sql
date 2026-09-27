-- A report template is data the company owns, not a file in this repo.
--
-- There are three template-shaped things in this product today and none of
-- them can see the other two:
--
--   1. `config/prompt_templates.json` in S3 -- one GLOBAL key, read by the
--      nightly lambda_report_generator, silently falling back to an in-code
--      schema when absent.
--   2. `src/report_templates/*.json` on disk -- read by report_template.py for
--      on-demand generation. Exactly one file exists.
--   3. `fs_templates_v1` in the browser's localStorage -- the "Template
--      Library" the UI has shipped since Sprint 10. Nothing server-side has
--      ever heard of it, so a template survives exactly as long as the tab
--      that made it, and is invisible to everyone else in the company.
--
-- This migration is (3) becoming real. It deliberately does NOT touch (1) or
-- (2): the disk templates stay loadable by id+version, because a report names
-- the template it was written to and report_template.TemplateNotFound exists
-- precisely so that name can never quietly become a lie.
--
-- THE BODY COLUMN IS THE SHAPE report_template.render_prompt ALREADY EATS
-- ({sections:[{key,title,purpose}], catch_all, excluded_subjects, style}).
-- Not a new dialect that something would have to translate. render_prompt is
-- unchanged by this migration and must stay that way: the moment a second
-- shape exists, "which template wrote this document" needs a translator to
-- answer, and translators drift.
--
-- VERSIONS ARE APPEND-ONLY AND NEVER REWRITTEN. A restore writes the old body
-- as a NEW version rather than moving a pointer backwards, which is what the
-- localStorage store already did (template-store.js restore()) and is the only
-- way the history of a document's template stays answerable. A report records
-- template_id + version; if a version could be edited in place, that record
-- would name a body that no longer exists.
--
-- ARCHIVE, NEVER DELETE, for the same reason: a deleted template is still the
-- template that wrote last month's reports.

CREATE TABLE IF NOT EXISTS report_templates (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id      uuid NOT NULL REFERENCES companies(id),
  -- 'org'      -- the company's, managed by gm/admin, visible to everyone in it
  -- 'personal' -- one person's own, visible to nobody else (see the note below)
  scope           text NOT NULL CHECK (scope IN ('org', 'personal')),
  owner_user_id   uuid REFERENCES users(id),
  slug            text NOT NULL,
  name            text NOT NULL,
  description     text NOT NULL DEFAULT '',
  report_type     text NOT NULL
                  CHECK (report_type IN ('daily', 'weekly', 'monthly',
                                         'session', 'day', 'custom')),
  current_version integer NOT NULL DEFAULT 0,
  archived_at     timestamptz,
  created_by      uuid NOT NULL REFERENCES users(id),
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  -- An org template owned by a person, or a personal one owned by nobody, are
  -- both incoherent -- and both would silently change who can see the row.
  -- The database refuses them rather than trusting every call site to check.
  CONSTRAINT report_templates_owner_matches_scope
    CHECK ((scope = 'personal') = (owner_user_id IS NOT NULL))
);

-- Uniqueness is per (company, scope, owner) and applies only to LIVE rows:
-- archiving a template must free its slug, or a company that tidies up can
-- never reuse a name it once used. The coalesce gives org rows (owner NULL) a
-- single bucket to collide in -- NULLs do not compare equal in a plain unique
-- index, so without it two org templates could share a slug.
CREATE UNIQUE INDEX IF NOT EXISTS report_templates_slug_uq
  ON report_templates (
    company_id, scope,
    coalesce(owner_user_id, '00000000-0000-0000-0000-000000000000'::uuid),
    slug
  ) WHERE archived_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_report_templates_company
  ON report_templates (company_id) WHERE archived_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_report_templates_owner
  ON report_templates (owner_user_id) WHERE archived_at IS NULL;

COMMENT ON COLUMN report_templates.scope IS
  'org = the company''s, managed by gm/admin; personal = one user''s own, not '
  'visible to anyone else including gm/admin. Anyone may copy an org template '
  'into their own personal library and edit that copy.';
COMMENT ON COLUMN report_templates.current_version IS
  'The highest version in report_template_versions. 0 means the row exists but '
  'no body has been written yet -- distinct from a template whose body is empty.';
COMMENT ON COLUMN report_templates.archived_at IS
  'Soft delete. Versions are kept: a deleted template is still the template '
  'that wrote last month''s reports.';

CREATE TABLE IF NOT EXISTS report_template_versions (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  template_id  uuid NOT NULL REFERENCES report_templates(id) ON DELETE CASCADE,
  version      integer NOT NULL CHECK (version > 0),
  body         jsonb NOT NULL,
  change_note  text,
  created_by   uuid NOT NULL REFERENCES users(id),
  created_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (template_id, version)
);

CREATE INDEX IF NOT EXISTS idx_report_template_versions_template
  ON report_template_versions (template_id, version DESC);

COMMENT ON TABLE report_template_versions IS
  'Append-only. A restore writes the old body as a NEW version rather than '
  'moving a pointer back, so a report recording template_id + version always '
  'names a body that still exists exactly as it was.';
COMMENT ON COLUMN report_template_versions.body IS
  'The shape report_template.render_prompt already consumes: '
  '{sections:[{key,title,purpose}], catch_all, excluded_subjects, style}.';

-- WHICH TEMPLATE THE SCHEDULE USES. One row per (company, report_type), and
-- only gm/admin may write it -- the one piece of template state that is not
-- the individual's to choose, because the nightly reports go to customers
-- under the company's name.
--
-- ON DELETE RESTRICT, not CASCADE: a binding vanishing because somebody
-- archived a template is exactly the silent fallback-to-default this design
-- refuses. Archiving a bound template has to fail loudly and make someone
-- choose a replacement.
CREATE TABLE IF NOT EXISTS report_template_bindings (
  company_id     uuid NOT NULL REFERENCES companies(id),
  report_type    text NOT NULL CHECK (report_type IN ('daily', 'weekly', 'monthly')),
  template_id    uuid NOT NULL REFERENCES report_templates(id) ON DELETE RESTRICT,
  -- NULL follows the template's current_version; a number pins it, so a
  -- company can keep issuing last quarter's format while editing the next one.
  pinned_version integer,
  set_by         uuid NOT NULL REFERENCES users(id),
  set_at         timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (company_id, report_type)
);

COMMENT ON TABLE report_template_bindings IS
  'Which template the nightly daily/weekly/monthly run uses, per company. '
  'gm/admin only. ReportGeneratorFunction is not in the VPC and cannot read '
  'this table directly -- org-api publishes a snapshot for it.';
