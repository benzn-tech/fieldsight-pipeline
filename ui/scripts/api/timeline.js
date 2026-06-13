/* ==========================================================================
   FieldSight API · Timeline (Daily Report) — BACKEND-CONTEXT §4.4 / §5.1
   --------------------------------------------------------------------------
   GET /api/timeline?date=YYYY-MM-DD&user=<Folder_Name>
     → DailyReport JSON (§5.1) OR
     → 404-body { message, date }                       (no report)
     → 200-body { date, available_users:[...] }         (admin disambig)
   ========================================================================== */

(function () {
  'use strict';

  function fixtures() {
    return (window.FieldSight && window.FieldSight.fixtures) || {};
  }

  /* Look up a fixture report by (date, folder) — fixtures keyed by both
     forms (Jarley_Trainor and Jarley Trainor). */
  function lookupReport(date, user) {
    var byDate = (fixtures().reports || {})[date];
    if (!byDate) return null;
    if (!user) return byDate.__summary || null;
    var folder = window.FS.api.folderName(user);
    return byDate[folder] || byDate[user] || null;
  }

  async function getTimeline(opts) {
    opts = opts || {};
    if (!window.FS.api.useMocks) {
      return window.FS.api.request('/timeline', {
        params: { date: opts.date, user: opts.user },
      });
    }
    await window.FS.api.delay(120);

    var date = opts.date;
    var user = opts.user;
    var caller = (window.AuthMock && window.AuthMock.currentUser) || {};

    /* Worker rule: server forces user = self (BACKEND-CONTEXT §3, §8.5). */
    if (caller.role === 'worker') {
      user = window.FS.api.folderName(caller.name);
    }

    var report = lookupReport(date, user);
    if (report) return report;

    /* Admin/gm with no user param → either summary or available_users.
       Sprint 2.1 fixtures don't ship a summary, so surface picker shape. */
    if (!user && (caller.role === 'admin' || caller.role === 'gm' || caller.isAdmin)) {
      var byDate = (fixtures().reports || {})[date] || {};
      var folders = Object.keys(byDate).filter(function (k) { return k.charAt(0) !== '_'; });
      if (folders.length > 0) {
        return { date: date, available_users: folders };
      }
    }

    /* No report — return the 404-body shape (consumers should also handle
       a real 404 with the same body, see BUG-20). */
    return {
      _notFound: true,
      message:   'No report for ' + (user || '(unknown)') + ' on ' + date,
      date:      date,
    };
  }

  window.FS.api.timeline = { getTimeline: getTimeline };

})();
