/* ==========================================================================
   FieldSight API · Action items — BACKEND-CONTEXT §4.10
   --------------------------------------------------------------------------
   GET  /api/actions?date=YYYY-MM-DD                → { date, actions: { '<topic_id>_<action_index>': { checked, checked_by, checked_at } } }
   POST /api/actions/toggle  body { date, topic_id, action_index, checked, action_text }

   Backed by an in-memory copy of fixtures.actions during Sprint 2.1, so
   mutations persist for the lifetime of the page (good enough to demo
   the optimistic-update pattern).
   ========================================================================== */

(function () {
  'use strict';

  /* Mutable copy — initial state seeded from fixtures the first time. */
  var state = null;

  function ensureState() {
    if (state) return;
    var f = (window.FieldSight && window.FieldSight.fixtures && window.FieldSight.fixtures.actions) || {};
    state = JSON.parse(JSON.stringify(f));
  }

  function actionKey(topic_id, action_index) {
    return topic_id + '_' + action_index;
  }

  async function getActions(date) {
    if (!window.FS.api.useMocks) {
      return window.FS.api.request('/actions', { params: { date: date } });
    }
    await window.FS.api.delay();
    ensureState();
    return { date: date, actions: state[date] || {} };
  }

  async function toggleAction(opts) {
    opts = opts || {};
    if (!window.FS.api.useMocks) {
      return window.FS.api.request('/actions/toggle', {
        method: 'POST',
        body: {
          date:         opts.date,
          topic_id:     opts.topic_id,
          action_index: opts.action_index,
          checked:      opts.checked,
          action_text:  opts.action_text,
        },
      });
    }
    await window.FS.api.delay(60);
    ensureState();

    var date  = opts.date;
    var key   = actionKey(opts.topic_id, opts.action_index);
    var who   = (window.AuthMock && window.AuthMock.currentUser && window.AuthMock.currentUser.name) || 'system';

    if (!state[date]) state[date] = {};
    state[date][key] = {
      checked:    !!opts.checked,
      checked_by: who,
      checked_at: new Date().toISOString(),
    };

    return { message: 'Updated', checked: !!opts.checked };
  }

  /* Sprint 4.2 — additive helper for cross-day audit aggregation.
     Backend exposes only single-date /api/actions (BACKEND-CONTEXT
     §4.10); this wraps a Promise.all over a date range and merges
     into a flat map keyed by date. PLAN.md Q-1 commits us to this
     fan-out approach for the prototype; a future
     `/api/actions/all?from=&to=` aggregator can drop in behind the
     same return shape. */
  async function getActionsRange(opts) {
    opts = opts || {};
    var from = opts.from, to = opts.to;
    if (!from || !to) return { byDate: {}, dates: [] };

    /* Build the date list inclusive (UTC arithmetic — BUG-19 safe). */
    var dates = [];
    var cursor = from;
    while (cursor <= to) {
      dates.push(cursor);
      cursor = window.FS.api.addDaysISO(cursor, 1);
    }

    var perDay = await Promise.all(dates.map(function (d) {
      return getActions(d).then(function (res) { return { date: d, res: res }; });
    }));

    var byDate = {};
    var anyDenied = null;
    perDay.forEach(function (x) {
      if (x.res && x.res._accessDenied) { anyDenied = x.res; return; }
      byDate[x.date] = (x.res && x.res.actions) || {};
    });

    if (anyDenied) {
      return { _accessDenied: true, error: anyDenied.error || 'Access denied' };
    }
    return { byDate: byDate, dates: dates };
  }

  window.FS.api.actions = {
    getActions:      getActions,
    getActionsRange: getActionsRange,
    toggleAction:    toggleAction,
    actionKey:       actionKey,
  };

})();
