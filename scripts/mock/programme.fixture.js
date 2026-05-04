/* ==========================================================================
   FieldSight Programme fixture (Sprint 4.4 — Programme MVP)
   --------------------------------------------------------------------------
   One programme for the SB1108 Ellesmere College site, Q2 2026.
   Schema documented in the Sprint 4 plan and on the page surface.

   Hierarchy: depth-2 WBS
     1.0 Earthworks & Foundations
       1.1 Site clearance              T-001
       1.2 Bulk excavation             T-002
       1.3 Foundation pour             T-003
     2.0 Structure
       2.1 Steel frame                 T-004
       2.2 Concrete columns            T-005
       2.3 Roof structure              T-006
     3.0 Envelope
       3.1 Cladding                    T-007
       3.2 Glazing                     T-008
       3.3 Roofing membrane            T-009
     4.0 Services
       4.1 Electrical rough-in         T-010
       4.2 Plumbing rough-in           T-011
       4.3 HVAC                        T-012
     5.0 Fit-out
       5.1 Internal walls              T-013
       5.2 Painting                    T-014

   Critical path: T-001 → T-002 → T-003 → T-004 → T-006 → T-009
                  → T-013 → T-014  (8 tasks, schedule-driving)

   Linked report actions (all from 2026-04-29 fixture report) wired
   onto a few mid-programme tasks so the right-pane "Linked actions"
   block has something concrete to render.
   ========================================================================== */

(function () {
  'use strict';

  /* Parents — render summary bars in the Gantt rolled up from
     children. parent_id null. Status = 'group'. */
  var parents = [
    { task_id: 'T-100', wbs: '1.0', name: 'Earthworks & Foundations',
      parent_id: null, status: 'group' },
    { task_id: 'T-200', wbs: '2.0', name: 'Structure',
      parent_id: null, status: 'group' },
    { task_id: 'T-300', wbs: '3.0', name: 'Envelope',
      parent_id: null, status: 'group' },
    { task_id: 'T-400', wbs: '4.0', name: 'Services',
      parent_id: null, status: 'group' },
    { task_id: 'T-500', wbs: '5.0', name: 'Fit-out',
      parent_id: null, status: 'group' },
  ];

  /* Leaf tasks. start/end inclusive, ISO date strings. */
  var leaves = [
    /* Earthworks (already done) */
    {
      task_id: 'T-001', wbs: '1.1', parent_id: 'T-100',
      name: 'Site clearance',
      start: '2026-04-01', end: '2026-04-07',
      duration_days: 7, progress_pct: 100, status: 'completed',
      depends_on: [],
      assignees: ['Jarley_Trainor', 'Ben_Lin'],
      resource_pool: ['Crew-A'],
      linked_action_items: [
        { date: '2026-04-29', topic_id: 0, action_index: 0 },
      ],
      tags: ['safety_critical'],
    },
    {
      task_id: 'T-002', wbs: '1.2', parent_id: 'T-100',
      name: 'Bulk excavation',
      start: '2026-04-08', end: '2026-04-15',
      duration_days: 8, progress_pct: 100, status: 'completed',
      depends_on: ['T-001'],
      assignees: ['Jarley_Trainor', 'David_Barillaro'],
      resource_pool: ['Crew-A'],
      linked_action_items: [],
      tags: [],
    },
    /* Foundations — wraps up TODAY-ish (2026-05-01) */
    {
      task_id: 'T-003', wbs: '1.3', parent_id: 'T-100',
      name: 'Foundation pour',
      /* Extended end-date by one day to 2026-05-01 so the
         Today × Programme integration (Sprint 4.10) showcases both
         a wrap-up task (T-003 at 95%) and a kickoff task (T-004
         at 12%) on the demo date. */
      start: '2026-04-16', end: '2026-05-01',
      duration_days: 16, progress_pct: 95, status: 'in_progress',
      depends_on: ['T-002'],
      assignees: ['Jarley_Trainor', 'Sarah_Chen'],
      resource_pool: ['Crew-A', 'Subcontractor-Concrete'],
      linked_action_items: [
        { date: '2026-04-29', topic_id: 2, action_index: 0 },
      ],
      tags: ['safety_critical'],
    },
    /* Structure — kicking off */
    {
      task_id: 'T-004', wbs: '2.1', parent_id: 'T-200',
      name: 'Steel frame',
      start: '2026-05-01', end: '2026-05-15',
      duration_days: 15, progress_pct: 12, status: 'in_progress',
      depends_on: ['T-003'],
      assignees: ['Jarley_Trainor', 'Ben_Lin', 'David_Barillaro'],
      resource_pool: ['Crew-B', 'Subcontractor-Steel'],
      linked_action_items: [
        { date: '2026-04-29', topic_id: 1, action_index: 0 },
      ],
      tags: ['safety_critical'],
    },
    {
      task_id: 'T-005', wbs: '2.2', parent_id: 'T-200',
      name: 'Concrete columns',
      start: '2026-05-04', end: '2026-05-12',
      duration_days: 9, progress_pct: 0, status: 'not_started',
      depends_on: ['T-003'],
      assignees: ['Sarah_Chen'],
      resource_pool: ['Crew-B'],
      linked_action_items: [
        { date: '2026-04-29', topic_id: 3, action_index: 0 },
      ],
      tags: [],
    },
    {
      task_id: 'T-006', wbs: '2.3', parent_id: 'T-200',
      name: 'Roof structure',
      start: '2026-05-16', end: '2026-05-25',
      duration_days: 10, progress_pct: 0, status: 'not_started',
      depends_on: ['T-004'],
      assignees: ['Jarley_Trainor', 'Ben_Lin'],
      resource_pool: ['Crew-B', 'Subcontractor-Steel'],
      linked_action_items: [],
      tags: ['safety_critical'],
    },
    /* Envelope */
    {
      task_id: 'T-007', wbs: '3.1', parent_id: 'T-300',
      name: 'Cladding',
      start: '2026-05-26', end: '2026-06-08',
      duration_days: 14, progress_pct: 0, status: 'not_started',
      depends_on: ['T-006'],
      assignees: ['Ben_Lin'],
      resource_pool: ['Crew-C', 'Subcontractor-Cladding'],
      linked_action_items: [],
      tags: [],
    },
    {
      task_id: 'T-008', wbs: '3.2', parent_id: 'T-300',
      name: 'Glazing',
      start: '2026-05-26', end: '2026-06-05',
      duration_days: 11, progress_pct: 0, status: 'not_started',
      depends_on: ['T-006'],
      assignees: ['David_Barillaro'],
      resource_pool: ['Subcontractor-Glazing'],
      linked_action_items: [],
      tags: [],
    },
    {
      task_id: 'T-009', wbs: '3.3', parent_id: 'T-300',
      name: 'Roofing membrane',
      start: '2026-05-26', end: '2026-06-02',
      duration_days: 8, progress_pct: 0, status: 'not_started',
      depends_on: ['T-006'],
      assignees: ['Sarah_Chen'],
      resource_pool: ['Subcontractor-Roof'],
      linked_action_items: [],
      tags: ['safety_critical'],
    },
    /* Services — overlap with envelope */
    {
      task_id: 'T-010', wbs: '4.1', parent_id: 'T-400',
      name: 'Electrical rough-in',
      start: '2026-05-15', end: '2026-05-30',
      duration_days: 16, progress_pct: 0, status: 'not_started',
      depends_on: ['T-005'],
      assignees: ['Ben_Lin'],
      resource_pool: ['Subcontractor-Elec'],
      linked_action_items: [],
      tags: [],
    },
    {
      task_id: 'T-011', wbs: '4.2', parent_id: 'T-400',
      name: 'Plumbing rough-in',
      start: '2026-05-15', end: '2026-05-28',
      duration_days: 14, progress_pct: 0, status: 'not_started',
      depends_on: ['T-005'],
      assignees: ['David_Barillaro'],
      resource_pool: ['Subcontractor-Plumb'],
      linked_action_items: [],
      tags: [],
    },
    {
      task_id: 'T-012', wbs: '4.3', parent_id: 'T-400',
      name: 'HVAC',
      start: '2026-05-20', end: '2026-06-08',
      duration_days: 20, progress_pct: 0, status: 'not_started',
      depends_on: ['T-005'],
      assignees: ['Sarah_Chen'],
      resource_pool: ['Subcontractor-Mech'],
      linked_action_items: [],
      tags: [],
    },
    /* Fit-out — final stretch */
    {
      task_id: 'T-013', wbs: '5.1', parent_id: 'T-500',
      name: 'Internal walls',
      start: '2026-06-09', end: '2026-06-22',
      duration_days: 14, progress_pct: 0, status: 'not_started',
      depends_on: ['T-007', 'T-009', 'T-010'],
      assignees: ['Jarley_Trainor'],
      resource_pool: ['Crew-D'],
      linked_action_items: [],
      tags: [],
    },
    {
      task_id: 'T-014', wbs: '5.2', parent_id: 'T-500',
      name: 'Painting',
      start: '2026-06-23', end: '2026-06-30',
      duration_days: 8, progress_pct: 0, status: 'not_started',
      depends_on: ['T-013'],
      assignees: ['Ben_Lin', 'David_Barillaro'],
      resource_pool: ['Subcontractor-Paint'],
      linked_action_items: [],
      tags: [],
    },

    /* ----------------------------------------------------------------------
       Sprint 8.9.1 — fixture variants for richer demos:
         T-021 — overdue (end-date in the past, status not 'done')
         T-022 — blocked (status 'blocked', user double-booked vs T-005)
         T-031 / T-032 — completed sub-group so the Board "Done" column
                          renders real work instead of an empty state
       Hung off existing parents to avoid breaking the WBS hierarchy.
       ---------------------------------------------------------------------- */
    {
      task_id: 'T-021', wbs: '1.4', parent_id: 'T-100',
      name: 'Drainage rough-in (overdue)',
      start: '2026-04-12', end: '2026-04-22',
      duration_days: 11, progress_pct: 60, status: 'in_progress',
      depends_on: ['T-002'],
      assignees: ['David_Barillaro'],
      resource_pool: ['Subcontractor-Drain'],
      linked_action_items: [],
      tags: ['overdue'],
    },
    {
      task_id: 'T-022', wbs: '2.4', parent_id: 'T-200',
      name: 'Anchor bolt set-out (blocked — awaiting survey)',
      start: '2026-05-04', end: '2026-05-08',
      duration_days: 5, progress_pct: 10, status: 'blocked',
      depends_on: ['T-003'],
      assignees: ['Sarah_Chen'],
      resource_pool: ['Crew-B'],
      linked_action_items: [],
      tags: ['blocked'],
    },
    {
      task_id: 'T-031', wbs: '1.5', parent_id: 'T-100',
      name: 'Survey peg & site setout',
      start: '2026-04-01', end: '2026-04-04',
      duration_days: 4, progress_pct: 100, status: 'completed',
      depends_on: [],
      assignees: ['Jarley_Trainor'],
      resource_pool: ['Surveyor'],
      linked_action_items: [],
      tags: [],
    },
    {
      task_id: 'T-032', wbs: '1.6', parent_id: 'T-100',
      name: 'Temporary fencing & site signage',
      start: '2026-04-02', end: '2026-04-06',
      duration_days: 5, progress_pct: 100, status: 'completed',
      depends_on: ['T-031'],
      assignees: ['Ben_Lin'],
      resource_pool: ['Crew-A'],
      linked_action_items: [],
      tags: [],
    },
  ];

  /* Apply baseline = planned (i.e. the original plan matches current
     dates — no slippage yet for the prototype). Real backend would
     diff baseline vs current to flag delays. */
  leaves.forEach(function (t) {
    t.baseline_start = t.start;
    t.baseline_end   = t.end;
  });

  var programme = {
    programme_id:        'sb1108-2026-q2',
    name:                'SB1108 Ellesmere College — Q2 2026',
    site_id:             'sb1108-ellesmere',
    start_date:          '2026-04-01',
    end_date:            '2026-06-30',
    baseline_start_date: '2026-04-01',
    baseline_end_date:   '2026-06-30',
    /* Schedule-driving chain — fixture-computed, not derived at runtime.
       Real backend would recompute on every task update via standard
       CPM (Critical Path Method). */
    critical_path: ['T-001', 'T-002', 'T-003', 'T-004', 'T-006',
                    'T-009', 'T-013', 'T-014'],
    tasks: parents.concat(leaves),
  };

  if (!window.FieldSight) window.FieldSight = {};
  if (!window.FieldSight.fixtures) window.FieldSight.fixtures = {};
  window.FieldSight.fixtures.programme = programme;

})();
