/* ==========================================================================
   FieldSight Fixtures · Calendar dates
   --------------------------------------------------------------------------
   Heat-map fixture for /api/dates (BACKEND-CONTEXT §4.3). Spans the trailing
   ~6 weeks ending on the today-fixture date 2026-04-29 — close enough for
   a 3-month dot-density preview without being noisy.

   Counts:
     - topics  = max across accessible users on that date
     - safety  = count of topics with category=='safety' OR
                 non-empty safety_flags[]

   Exported to window.FieldSight.fixtures.dates = { dates: { 'YYYY-MM-DD': {…} } }
   ========================================================================== */

(function () {
  'use strict';

  function addDays(yyyymmdd, days) {
    var p = yyyymmdd.split('-').map(Number);
    var d = new Date(Date.UTC(p[0], p[1] - 1, p[2] + days));
    return d.toISOString().slice(0, 10);
  }

  /* Hand-tuned dot density — busy mid-week, lighter on weekends.
     Sprint 8.9.1 — full April 2026 (30 days, 2026-04-01 → 2026-04-30):
       18 days with full reports (kind: 'full')
       8 days with meeting-minutes only (kind: 'minutes', no daily report)
       4 days empty (kind: 'empty', omitted from the map entirely) */
  var BASE = '2026-04-29';
  var PROFILE = [
    /* d = days offset back from BASE. d=-1 → 2026-04-30, d=28 → 2026-04-01. */
    { d: -1, kind: 'minutes', topics: 3,  safety: 0 }, /* 04-30 Thu — minutes only */
    { d: 0,  kind: 'full',    topics: 11, safety: 2 }, /* 04-29 Wed — today */
    { d: 1,  kind: 'full',    topics: 14, safety: 3 }, /* 04-28 Tue */
    { d: 2,  kind: 'full',    topics: 9,  safety: 1 }, /* 04-27 Mon */
    { d: 3,  kind: 'full',    topics: 12, safety: 2 }, /* 04-26 Sun (catch-up) */
    { d: 4,  kind: 'minutes', topics: 4,  safety: 0 }, /* 04-25 Sat — minutes only */
    { d: 5,  kind: 'empty' },                          /* 04-24 Fri — empty */
    { d: 6,  kind: 'full',    topics: 13, safety: 4 }, /* 04-23 Thu — high-risk */
    { d: 7,  kind: 'full',    topics: 11, safety: 1 }, /* 04-22 Wed */
    { d: 8,  kind: 'minutes', topics: 5,  safety: 0 }, /* 04-21 Tue — minutes only */
    { d: 9,  kind: 'full',    topics: 8,  safety: 0 }, /* 04-20 Mon — all clear */
    { d: 10, kind: 'full',    topics: 12, safety: 2 }, /* 04-19 Sun */
    { d: 11, kind: 'minutes', topics: 4,  safety: 0 }, /* 04-18 Sat — minutes only */
    { d: 12, kind: 'empty' },                          /* 04-17 Fri — empty */
    { d: 13, kind: 'full',    topics: 9,  safety: 1 }, /* 04-16 Thu */
    { d: 14, kind: 'full',    topics: 11, safety: 3 }, /* 04-15 Wed */
    { d: 15, kind: 'full',    topics: 13, safety: 2 }, /* 04-14 Tue */
    { d: 16, kind: 'full',    topics: 7,  safety: 1 }, /* 04-13 Mon */
    { d: 17, kind: 'minutes', topics: 3,  safety: 0 }, /* 04-12 Sun — minutes only */
    { d: 18, kind: 'full',    topics: 12, safety: 1 }, /* 04-11 Sat (catch-up) */
    { d: 19, kind: 'minutes', topics: 4,  safety: 0 }, /* 04-10 Fri — minutes only */
    { d: 20, kind: 'empty' },                          /* 04-09 Thu — empty */
    { d: 21, kind: 'full',    topics: 9,  safety: 2 }, /* 04-08 Wed */
    { d: 22, kind: 'full',    topics: 14, safety: 4 }, /* 04-07 Tue — high-risk */
    { d: 23, kind: 'full',    topics: 8,  safety: 1 }, /* 04-06 Mon */
    { d: 24, kind: 'minutes', topics: 5,  safety: 0 }, /* 04-05 Sun — minutes only */
    { d: 25, kind: 'full',    topics: 10, safety: 0 }, /* 04-04 Sat */
    { d: 26, kind: 'full',    topics: 11, safety: 2 }, /* 04-03 Fri */
    { d: 27, kind: 'empty' },                          /* 04-02 Thu — empty */
    { d: 28, kind: 'minutes', topics: 6,  safety: 0 }, /* 04-01 Wed — minutes only */
  ];

  var dates = {};
  PROFILE.forEach(function (entry) {
    if (entry.kind === 'empty') return;
    var key = addDays(BASE, -entry.d);
    dates[key] = {
      hasReport:     entry.kind === 'full',
      hasMinutes:    entry.kind === 'minutes',
      kind:          entry.kind,
      topics:        entry.topics,
      safety:        entry.safety,
    };
  });

  if (!window.FieldSight) window.FieldSight = {};
  if (!window.FieldSight.fixtures) window.FieldSight.fixtures = {};
  window.FieldSight.fixtures.dates = { dates: dates };

})();
