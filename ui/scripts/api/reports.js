/* ==========================================================================
   FieldSight API · Reports archive — BACKEND-CONTEXT §4.11
   --------------------------------------------------------------------------
   GET  /api/reports/history?limit=20      → { reports: [{ key, type, date, generated_at, size }] }
   POST /api/reports/generate body { report_type, date?, force? }
                                           → 202 { message, status:'pending' }
   ========================================================================== */

(function () {
  'use strict';

  function fixtures() {
    return (window.FieldSight && window.FieldSight.fixtures) || {};
  }

  async function getReportsHistory(limit) {
    if (!window.FS.api.useMocks) {
      return window.FS.api.request('/reports/history', { params: { limit: limit || 20 } });
    }
    await window.FS.api.delay();
    var rows = (fixtures().reportHistory || []).slice(0, limit || 20);
    return { reports: rows };
  }

  async function regenerate(opts) {
    opts = opts || {};
    if (!window.FS.api.useMocks) {
      return window.FS.api.request('/reports/generate', {
        method: 'POST',
        body: {
          report_type: opts.report_type,
          date:        opts.date,
          force:       !!opts.force,
        },
      });
    }
    await window.FS.api.delay(150);
    return {
      message: 'Regeneration of ' + (opts.report_type || 'daily') + ' for ' + (opts.date || 'latest') + ' queued.',
      status:  'pending',
    };
  }

  window.FS.api.reports = {
    getReportsHistory: getReportsHistory,
    regenerate:        regenerate,
  };

})();
