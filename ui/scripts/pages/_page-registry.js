/* ==========================================================================
   FieldSight Page Registry — route → { Middle, Right }
   --------------------------------------------------------------------------
   Each page module attaches itself to window.FieldSight.PAGES.<key>
   AFTER this registry loads. The registry just defines the mapping.

   Exported to:
     window.FieldSight.getPageForRoute(routePath) → { Middle, Right } | null
   ========================================================================== */

(function () {
  'use strict';

  /* Pages register themselves via this object after load */
  if (!window.FieldSight) window.FieldSight = {};
  if (!window.FieldSight.PAGES) window.FieldSight.PAGES = {};

  /* Resolve a route to { Middle, Right } components */
  function getPageForRoute(routePath) {
    var pages = window.FieldSight.PAGES || {};
    /* Direct match */
    if (pages[routePath]) return pages[routePath];
    /* No match — fall through to placeholder */
    return null;
  }

  window.FieldSight.getPageForRoute = getPageForRoute;
})();
