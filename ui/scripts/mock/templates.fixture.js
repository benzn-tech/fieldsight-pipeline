/* ==========================================================================
   FieldSight Fixtures · Templates — Sprint 10 B.0
   --------------------------------------------------------------------------
   Per-company report template library fixture.

   Shape mirrors the LandingAI ADE extraction output so the real backend
   swap-in (Sprint 11+) is backend-only — no UI rework needed.

   Template: { id, scope, report_type, active, owner_user_id, title,
               description, created_at, versions[] }
   Version:  { id, schema, created_at, created_by_user_id, change_note }
   Schema:   { sections: [{ title, kind, fields, prompt_hint }] }
   kind ∈ { narrative, list, table, kpi, photos }

   Org templates (3) — visible to all users, managed by admin/gm/director.
   Personal templates — per-user, visible only to their owner.

   Exported to window.FieldSight.fixtures.templates = { org, personal }
   ========================================================================== */

(function () {
  'use strict';

  var ORG_TEMPLATES = [
    {
      id:            'tpl-org-daily-standard',
      scope:         'org',
      report_type:   'daily',
      active:        true,
      owner_user_id: null,
      title:         'Daily Report — Standard',
      description:   'Standard FieldSight daily site report covering progress, workforce, safety, and open actions.',
      created_at:    '2026-01-15T08:00:00.000Z',
      versions: [
        {
          id:                  'ver-org-daily-1',
          schema: {
            sections: [
              { title: 'Daily Summary',   kind: 'narrative', fields: [],                                             prompt_hint: 'Summarise the day\'s key activities and overall progress' },
              { title: 'Workforce',        kind: 'kpi',      fields: ['headcount', 'subcontractors', 'visitors'],   prompt_hint: 'Labour numbers on site, including subcontractor and visitor counts' },
              { title: 'Key Decisions',   kind: 'list',      fields: [],                                             prompt_hint: 'Decisions made today that affect programme or cost' },
              { title: 'Open Actions',    kind: 'table',     fields: ['action', 'owner', 'due_date'],               prompt_hint: 'Outstanding tasks assigned from today\'s report' },
              { title: 'Safety Notes',    kind: 'narrative', fields: [],                                             prompt_hint: 'Safety observations, near-misses, and HSE items from the day' },
              { title: 'Photos',          kind: 'photos',    fields: [],                                             prompt_hint: 'Progress and site condition photos' },
            ],
          },
          created_at:           '2026-01-15T08:00:00.000Z',
          created_by_user_id:   'system',
          change_note:          'Initial template',
        },
      ],
    },

    {
      id:            'tpl-org-weekly-progress',
      scope:         'org',
      report_type:   'weekly',
      active:        true,
      owner_user_id: null,
      title:         'Weekly Progress Report',
      description:   'Programme completion summary, variance analysis, issues log, and week-ahead preview for client distribution.',
      created_at:    '2026-01-15T08:00:00.000Z',
      versions: [
        {
          id:                  'ver-org-weekly-1',
          schema: {
            sections: [
              { title: 'Executive Summary',  kind: 'narrative', fields: [],                                              prompt_hint: 'One-paragraph progress summary for client distribution' },
              { title: 'Programme KPIs',     kind: 'kpi',       fields: ['completion_pct', 'days_variance', 'rfi_open'], prompt_hint: 'Key metrics versus baseline programme' },
              { title: 'Completed This Week',kind: 'list',      fields: [],                                              prompt_hint: 'Programme tasks completed in the reporting week' },
              { title: 'Planned Next Week',  kind: 'list',      fields: [],                                              prompt_hint: 'Tasks scheduled for the coming week' },
              { title: 'Issues & Risks',     kind: 'table',     fields: ['issue', 'impact', 'mitigation'],               prompt_hint: 'Open issues with impact rating and mitigation plan' },
              { title: 'Photos',             kind: 'photos',    fields: [],                                              prompt_hint: 'Representative progress photos for the week' },
            ],
          },
          created_at:           '2026-01-15T08:00:00.000Z',
          created_by_user_id:   'system',
          change_note:          'Initial template',
        },
      ],
    },

    {
      id:            'tpl-org-incident-standard',
      scope:         'org',
      report_type:   'incident',
      active:        true,
      owner_user_id: null,
      title:         'Incident Report — Standard',
      description:   'HSWA 2015-aligned incident record with root cause analysis, corrective actions, and sign-off fields.',
      created_at:    '2026-01-15T08:00:00.000Z',
      versions: [
        {
          id:                  'ver-org-incident-1',
          schema: {
            sections: [
              { title: 'Incident Details',   kind: 'kpi',       fields: ['date_time', 'location', 'incident_type', 'severity'],     prompt_hint: 'Who, what, when, where — factual classification' },
              { title: 'Description',        kind: 'narrative', fields: [],                                                           prompt_hint: 'Factual account of what occurred, in chronological order' },
              { title: 'Immediate Actions',  kind: 'list',      fields: [],                                                           prompt_hint: 'Steps taken immediately after the incident' },
              { title: 'Root Cause',         kind: 'narrative', fields: [],                                                           prompt_hint: '5-Why or bow-tie analysis identifying root cause' },
              { title: 'Corrective Actions', kind: 'table',     fields: ['action', 'owner', 'due_date', 'status'],                   prompt_hint: 'Preventive and corrective actions with owners and target dates' },
              { title: 'Photos & Evidence',  kind: 'photos',    fields: [],                                                           prompt_hint: 'Scene photos, diagrams, and supporting evidence' },
            ],
          },
          created_at:           '2026-01-15T08:00:00.000Z',
          created_by_user_id:   'system',
          change_note:          'Initial template',
        },
      ],
    },
  ];

  /* Personal template — PM user James Lamb (device_id: Benl5) */
  var PERSONAL_TEMPLATES = [
    {
      id:            'tpl-personal-benl5-daily',
      scope:         'personal',
      report_type:   'daily',
      active:        false,
      owner_user_id: 'Benl5',
      title:         'PM Daily — Condensed',
      description:   'Shorter daily format for busy days. Omits photos; focuses on decisions and blockers.',
      created_at:    '2026-03-01T07:30:00.000Z',
      versions: [
        {
          id:                  'ver-personal-benl5-1',
          schema: {
            sections: [
              { title: 'Day Summary',    kind: 'narrative', fields: [],  prompt_hint: 'One paragraph — key achievements and headline activities' },
              { title: 'Blockers',       kind: 'list',      fields: [],  prompt_hint: 'Anything blocking progress with the person responsible for unblocking' },
              { title: 'Decisions Made', kind: 'list',      fields: [],  prompt_hint: 'Client or PM decisions made today that affect scope or programme' },
              { title: 'Tomorrow',       kind: 'list',      fields: [],  prompt_hint: 'Top three priorities for tomorrow' },
            ],
          },
          created_at:           '2026-03-01T07:30:00.000Z',
          created_by_user_id:   'Benl5',
          change_note:          'Created from scratch',
        },
      ],
    },
  ];

  if (!window.FieldSight)          window.FieldSight = {};
  if (!window.FieldSight.fixtures) window.FieldSight.fixtures = {};
  window.FieldSight.fixtures.templates = {
    org:      ORG_TEMPLATES,
    personal: PERSONAL_TEMPLATES,
  };

})();
