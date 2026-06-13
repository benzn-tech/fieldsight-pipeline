/* ==========================================================================
   FieldSight API · Calendar dates — BACKEND-CONTEXT §4.3
   --------------------------------------------------------------------------
   GET /api/dates?months=3&site=<site_id>  →  { dates: { 'YYYY-MM-DD': { hasReport, topics, safety } } }
   ========================================================================== */

(function () {
  'use strict';

  function fixtures() {
    return (window.FieldSight && window.FieldSight.fixtures) || {};
  }

  async function getDates(opts) {
    opts = opts || {};
    if (!window.FS.api.useMocks) {
      return window.FS.api.request('/dates', {
        params: { months: opts.months, site: opts.site },
      });
    }
    await window.FS.api.delay();
    var f = fixtures().dates || { dates: {} };
    /* Sprint 2.1: site filter is a no-op against the fixture. Real backend
       filters by accessible users on the requested site. */
    return { dates: f.dates, months: opts.months || 3, site: opts.site || null };
  }

  window.FS.api.dates = { getDates: getDates };

})();
