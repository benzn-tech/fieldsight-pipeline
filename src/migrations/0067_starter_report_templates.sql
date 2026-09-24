-- Four org templates every company starts with, as real rows.
--
-- These four have existed since Sprint 10 as `scripts/mock/templates.fixture.js`
-- -- a browser fixture that seeded localStorage. Nothing server-side ever heard
-- of them, so they were never a customer's templates; they were a demo of what
-- a template would look like if the feature existed. Migration 0062 made the
-- feature exist. This one makes the four real.
--
-- WHAT CHANGED ON THE WAY IN, because the fixture was written against a shape
-- that turned out not to be the shape:
--
--   prompt_hint -> purpose   render_prompt reads `purpose`; `prompt_hint` was
--                            the editor's own name for the same sentence.
--   fields      -> columns   `fields` was decoration: nothing server-side has
--                            ever read it. On the table sections it held
--                            exactly what `columns` now needs, so it carries
--                            over there and is dropped everywhere else rather
--                            than seeded as a field nothing consumes.
--   photos      -> gone      A generated report is prose; nothing in that path
--                            inserts an image. Seeding a Photos section into
--                            every company would be seeding a promise this
--                            product cannot keep today. The owner removed them.
--                            "Photos & Evidence" survives in the incident
--                            template as a NARRATIVE section, because what it
--                            is for there is the written account of what
--                            evidence exists -- which the model can write.
--
-- EVERY SECTION'S `columns` IS SPELLED OUT ON THE TABLE ONES. A table section
-- that names no columns falls back to Item | Assigned | Due, and the fallback
-- exists for bodies written before the field did -- not as a way to avoid
-- deciding. These four were written after, so they decide.
--
-- WHAT THIS MIGRATION DOES NOT DO, said plainly because the gap is real:
--
--   * It seeds the companies that exist WHEN IT RUNS. A company created later
--     gets nothing. There is no trigger that could fix this: created_by is NOT
--     NULL and references users(id), and a company has no users at the instant
--     it is created. Seeding a new company is a call someone has to make, and
--     the place to make it is wherever companies are created.
--   * It binds nothing. `report_template_bindings` stays empty, so the nightly
--     run is unaffected -- and it would be unaffected regardless, because
--     lambda_report_generator has zero references to that table.
--   * It activates nothing. These are four templates a company can pick from,
--     not a new default anybody's existing reports switch to.
--
-- Idempotent on (company_id, scope, slug) among live rows, which is the same
-- key report_templates_slug_uq enforces. Re-running adds nothing; a company
-- that edited or archived one of these keeps its own decision.

-- `incident` was not in 0062's report_type CHECK, because the only templates
-- that existed then were daily/weekly/monthly/session/day. An incident report
-- is a real report type this product means -- calling it 'custom' to fit the
-- constraint would throw away the one thing the column is for. The bindings
-- table keeps its narrower check (daily/weekly/monthly): an incident report is
-- never a nightly run, and nothing should be able to schedule one.
ALTER TABLE report_templates DROP CONSTRAINT IF EXISTS report_templates_report_type_check;
ALTER TABLE report_templates ADD CONSTRAINT report_templates_report_type_check
  CHECK (report_type IN ('daily', 'weekly', 'monthly',
                         'session', 'day', 'custom', 'incident'));

-- THE BODIES LIVE HERE AND NOWHERE ELSE.
--
-- Two callers need them: this migration, for the companies that already exist,
-- and lambda_org_seed, for every company created from now on. A copy in Python
-- beside a copy in SQL is two copies, and the one that gets edited is not
-- reliably the one that runs. So the bodies live in a function, and both
-- callers call the function.
--
-- Changing a starter later means a new migration that CREATE OR REPLACEs this.
-- It does not reach back into templates already seeded, and that is correct:
-- by then they are the company's, not ours -- they may have been edited, and a
-- report that recorded (template, version) must keep naming a body that still
-- exists exactly as it was.
CREATE OR REPLACE FUNCTION seed_starter_report_templates(
  p_company uuid,
  p_author  uuid
) RETURNS integer
LANGUAGE plpgsql AS $fn$
DECLARE
  starter  record;
  tpl_id   uuid;
  n        integer := 0;
BEGIN
  IF p_company IS NULL OR p_author IS NULL THEN
    -- created_by is NOT NULL and references a real person. A company with
    -- nobody in it has no one to name and no one to see a template.
    RETURN 0;
  END IF;

  FOR starter IN
    SELECT * FROM (VALUES
        ('daily-report-standard', 'Daily Report — Standard', 'daily',
         'The standard end-of-day record: what happened, who was on site, what was decided, what is still open, and the safety and quality picture.',
         $body$
         {
           "sections": [
             {"key": "daily-summary", "title": "Daily Summary", "kind": "narrative",
              "purpose": "The day's key activities and overall progress."},
             {"key": "workforce", "title": "Workforce", "kind": "kpi",
              "purpose": "Labour numbers on site, including subcontractor and visitor counts."},
             {"key": "key-decisions", "title": "Key Decisions", "kind": "list",
              "purpose": "Decisions made today that affect programme or cost."},
             {"key": "open-actions", "title": "Open Actions", "kind": "table",
              "columns": ["Action", "Owner", "Due"],
              "purpose": "Outstanding tasks assigned from today."},
             {"key": "safety-notes", "title": "Safety Notes", "kind": "narrative",
              "purpose": "Safety observations, near-misses and HSE items from the day."},
             {"key": "quality-assurance", "title": "Quality Assurance", "kind": "list",
              "purpose": "Inspections, hold points, defects raised or closed, and anything awaiting sign-off."}
           ],
           "catch_all": {"key": "anything-else", "title": "Anything else",
             "purpose": "Anything the recording covered that none of the headings above account for."},
           "excluded_subjects": [],
           "style": []
         }
         $body$::jsonb),

        ('weekly-progress-report', 'Weekly Progress Report', 'weekly',
         'A client-facing week in review: headline progress, the numbers against baseline, what closed, what is next, and the open issues.',
         $body$
         {
           "sections": [
             {"key": "executive-summary", "title": "Executive Summary", "kind": "narrative",
              "purpose": "One paragraph of progress, written for distribution outside the site team."},
             {"key": "programme-kpis", "title": "Programme KPIs", "kind": "kpi",
              "purpose": "Key metrics against the baseline programme."},
             {"key": "completed-this-week", "title": "Completed This Week", "kind": "list",
              "purpose": "Programme tasks completed in the reporting week."},
             {"key": "planned-next-week", "title": "Planned Next Week", "kind": "list",
              "purpose": "Tasks scheduled for the coming week."},
             {"key": "issues-and-risks", "title": "Issues & Risks", "kind": "table",
              "columns": ["Issue", "Impact", "Mitigation"],
              "purpose": "Open issues, what each one is costing, and what is being done about it."}
           ],
           "catch_all": {"key": "anything-else", "title": "Anything else",
             "purpose": "Anything the recording covered that none of the headings above account for."},
           "excluded_subjects": [],
           "style": []
         }
         $body$::jsonb),

        ('incident-report-standard', 'Incident Report — Standard', 'incident',
         'A factual record of one incident: what happened, what was done at the time, why it happened, and what stops it happening again.',
         $body$
         {
           "sections": [
             {"key": "incident-details", "title": "Incident Details", "kind": "kpi",
              "purpose": "Who, what, when and where — the factual classification."},
             {"key": "description", "title": "Description", "kind": "narrative",
              "purpose": "A factual account of what occurred, in the order it occurred."},
             {"key": "immediate-actions", "title": "Immediate Actions", "kind": "list",
              "purpose": "Steps taken immediately after the incident."},
             {"key": "root-cause", "title": "Root Cause", "kind": "narrative",
              "purpose": "The analysis of why it happened, and what was ruled out on the way."},
             {"key": "corrective-actions", "title": "Corrective Actions", "kind": "table",
              "columns": ["Action", "Owner", "Due", "Status"],
              "purpose": "Preventive and corrective actions, with owners and target dates."},
             {"key": "photos-and-evidence", "title": "Photos & Evidence", "kind": "narrative",
              "purpose": "What evidence exists and where it is — photographs taken, diagrams, and anything kept for the record. Describe it; do not reproduce it."}
           ],
           "catch_all": {"key": "anything-else", "title": "Anything else",
             "purpose": "Anything the recording covered that none of the headings above account for."},
           "excluded_subjects": [],
           "style": []
         }
         $body$::jsonb),

        ('pm-daily-condensed', 'PM Daily — Condensed', 'daily',
         'A shorter daily format for busy days: what moved, what is stuck, what was decided, and what matters tomorrow.',
         $body$
         {
           "sections": [
             {"key": "day-summary", "title": "Day Summary", "kind": "narrative",
              "purpose": "One paragraph — the headline activities and what was achieved."},
             {"key": "blockers", "title": "Blockers", "kind": "list",
              "purpose": "Anything blocking progress, with the person it is waiting on."},
             {"key": "decisions-made", "title": "Decisions Made", "kind": "list",
              "purpose": "Client or PM decisions made today that affect scope or programme."},
             {"key": "tomorrow", "title": "Tomorrow", "kind": "list",
              "purpose": "The priorities for tomorrow."}
           ],
           "catch_all": {"key": "anything-else", "title": "Anything else",
             "purpose": "Anything the recording covered that none of the headings above account for."},
           "excluded_subjects": [],
           "style": []
         }
         $body$::jsonb)
    ) AS t(slug, name, report_type, description, body)
  LOOP
    -- Idempotent on the same key report_templates_slug_uq enforces. Re-running
    -- adds nothing, and a company that archived a starter does not have it
    -- reappear -- that was their decision about their own library.
    CONTINUE WHEN EXISTS (
      SELECT 1 FROM report_templates rt
       WHERE rt.company_id = p_company
         AND rt.scope = 'org'
         AND rt.slug = starter.slug
         AND rt.archived_at IS NULL
    );

    INSERT INTO report_templates
      (company_id, scope, owner_user_id, slug, name, description,
       report_type, current_version, created_by)
    VALUES
      (p_company, 'org', NULL, starter.slug, starter.name, starter.description,
       starter.report_type, 1, p_author)
    RETURNING id INTO tpl_id;

    INSERT INTO report_template_versions
      (template_id, version, body, change_note, created_by)
    VALUES
      (tpl_id, 1, starter.body, 'Starter template', p_author);

    n := n + 1;
  END LOOP;

  RETURN n;
END
$fn$;

COMMENT ON FUNCTION seed_starter_report_templates(uuid, uuid) IS
  'Gives one company its own copies of the four starter templates. Per company, '
  'never shared: each row carries that company_id, and editing one cannot touch '
  'another company. Idempotent, and silent for a company that has them or has '
  'archived them. Called here for the companies that existed when this ran, and '
  'by lambda_org_seed for every company created after.';

-- The companies that exist now.
DO $$
DECLARE
  co     record;
  author uuid;
BEGIN
  FOR co IN SELECT id FROM companies LOOP
    -- A company officer is preferred over an arbitrary member: these are org
    -- templates, and org templates are the officers' to manage.
    SELECT u.id INTO author
      FROM users u
     WHERE u.company_id = co.id
       AND u.archived_at IS NULL
     ORDER BY (u.global_role IN ('gm', 'admin')) DESC, u.created_at ASC
     LIMIT 1;

    PERFORM seed_starter_report_templates(co.id, author);
  END LOOP;
END $$;
