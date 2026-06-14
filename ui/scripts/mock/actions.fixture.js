/* ==========================================================================
   FieldSight Fixtures · Action items audit state
   --------------------------------------------------------------------------
   Mirrors GET /api/actions response shape — keyed first by date, then by
   `${topic_id}_${action_index}` (BACKEND-CONTEXT §4.10, §8.8).

   Note: topic_ids may shift if a report is regenerated (BUG §8.8). The
   fixture takes that as accepted risk; for hard audit, use the action
   history endpoint.

   Exported to window.FieldSight.fixtures.actions = { 'YYYY-MM-DD': { '<key>': {…} } }
   ========================================================================== */

(function () {
  'use strict';

  var actions = {
    '2026-04-29': {
      /* Topic 0 — Morning safety briefing — first action already done. */
      '0_0': {
        checked:    true,
        checked_by: 'Jack Gibson',
        checked_at: '2026-04-29T07:42:00Z',
      },
      /* Topic 2 — Concrete pour — slump test action checked off. */
      '2_1': {
        checked:    true,
        checked_by: 'Jarley Trainor',
        checked_at: '2026-04-29T01:18:42Z',
      },
    },
  };

  if (!window.FieldSight) window.FieldSight = {};
  if (!window.FieldSight.fixtures) window.FieldSight.fixtures = {};
  window.FieldSight.fixtures.actions = actions;

})();
