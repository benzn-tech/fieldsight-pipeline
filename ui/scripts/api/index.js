/* ==========================================================================
   FieldSight API namespace — Sprint 2.1 (Phase A)
   --------------------------------------------------------------------------
   One module per real backend endpoint, all under window.FS.api.*.

   Today every call is satisfied from in-memory fixtures (window.FieldSight
   .fixtures.*) with a small artificial delay to simulate network. When the
   real API is wired (Sprint I — see PLAN.md), flip `useMocks` to false and
   each module will fall through to a real `fetch()` against /api/<route>.

   The PUBLIC SHAPE of every response matches BACKEND-CONTEXT.md §4 verbatim.
   Call sites code against that shape, not against the fixtures.

   Loaded BEFORE any composite or page that calls FS.api.*.
   ========================================================================== */

(function () {
  'use strict';

  if (!window.FS) window.FS = {};

  /* Folder name in S3 = display name with spaces → underscores
     (BACKEND-CONTEXT §6, §4.4). Centralised so call sites don't hand-roll. */
  function folderName(displayName) {
    return (displayName || '').replace(/\s+/g, '_');
  }

  /* Small artificial delay so optimistic-UI patterns can be tested. */
  function delay(ms) {
    return new Promise(function (resolve) {
      setTimeout(resolve, ms || 80);
    });
  }

  /* Build a presigned-URL placeholder so UI code can render <img>/<video>
     against a plausible-looking string. Real backend returns short-lived
     S3 URLs from /api/media/presigned-url; replace when going live. */
  function mockPresignedUrl(key) {
    return 'https://mock.fieldsight.local/' + key + '?expires=900';
  }

  /* NZDT (UTC+13) date math helper — see BACKEND-CONTEXT §8.1 / BUG-19.
     `new Date('2026-04-29')` parses as UTC midnight; toISOString().slice(0,10)
     can shift the date by one day in NZDT. Use UTC-based arithmetic only. */
  function addDaysISO(yyyymmdd, days) {
    var parts = yyyymmdd.split('-').map(Number);
    var d = new Date(Date.UTC(parts[0], parts[1] - 1, parts[2] + days));
    return d.toISOString().slice(0, 10);
  }

  /* "Today" in NZDT (Pacific/Auckland) — Sprint 3 P-06.
     Uses Intl with the canonical IANA zone so DST transitions
     (NZDT/NZST) are handled by the runtime. Falls back to a manual
     UTC+13 offset if the engine is missing the time-zone tables. */
  function todayNZDT() {
    try {
      var fmt = new Intl.DateTimeFormat('en-CA', {
        timeZone: 'Pacific/Auckland',
        year: 'numeric', month: '2-digit', day: '2-digit',
      });
      return fmt.format(new Date()); /* en-CA → YYYY-MM-DD */
    } catch (e) {
      var d = new Date(Date.now() + 13 * 60 * 60 * 1000);
      return d.toISOString().slice(0, 10);
    }
  }

  /* Detect HTML-content-type 404 trap (BACKEND-CONTEXT §8.2 / BUG-20). */
  function isJsonResponse(res) {
    var ct = (res.headers.get && res.headers.get('content-type')) || '';
    return ct.indexOf('application/json') !== -1;
  }

  window.FS.api = {
    useMocks: true,
    baseUrl: '/api',
    delay: delay,
    folderName: folderName,
    mockPresignedUrl: mockPresignedUrl,
    addDaysISO: addDaysISO,
    todayNZDT: todayNZDT,
    isJsonResponse: isJsonResponse,
  };

})();
