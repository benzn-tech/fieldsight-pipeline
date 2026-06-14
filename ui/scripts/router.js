/* ==========================================================================
   FieldSight Router — hash-based, no build step
   window.FS.Router
   ========================================================================== */

(function () {
  'use strict';

  const subscribers = new Set();

  /** Parse location.hash into { path, params } */
  function parse() {
    const raw = (location.hash || '').replace(/^#\/?/, '');
    const [pathPart, queryPart] = raw.split('?');
    const path = '/' + (pathPart || '').replace(/^\//, '');
    const params = {};
    if (queryPart) {
      queryPart.split('&').forEach(pair => {
        const [k, v] = pair.split('=');
        if (k) params[decodeURIComponent(k)] = decodeURIComponent(v || '');
      });
    }
    return { path, params };
  }

  function notify() {
    const route = parse();
    subscribers.forEach(cb => { try { cb(route); } catch (e) { console.error('[Router]', e); } });
  }

  window.addEventListener('hashchange', notify);

  const Router = {
    /** @returns {{ path: string, params: Record<string,string> }} */
    getCurrentRoute() { return parse(); },

    /** Navigate to a path (e.g. '/today') */
    navigate(path) {
      const next = '#' + path;
      if (location.hash !== next) {
        location.hash = next;
      } else {
        // Same route — still notify so components can refresh
        notify();
      }
    },

    /** Subscribe to route changes. Callback called immediately with current route. */
    subscribe(callback) {
      subscribers.add(callback);
      callback(parse());           // emit current route immediately
      return () => subscribers.delete(callback);
    },

    /** Unsubscribe */
    unsubscribe(callback) { subscribers.delete(callback); },
  };

  // Attach to window.FS (may be extended later)
  if (!window.FS) window.FS = {};
  window.FS.Router = Router;

  /* Sprint 8.4.4 — Swipe-to-go-back gesture (mobile left-edge swipe).
     Fires history.back() when:
       • touchstart begins within 20px of the left edge
       • touch travels > 80px to the right
       • average velocity > 0.3 px/ms
       • the mobile right-detail panel is open (FS.shell exists + has selection)
     We check FS.shell at dispatch time (set by AppShell after mount). */
  (function () {
    var startX = 0;
    var startY = 0;
    var startTime = 0;
    var tracking = false;

    document.addEventListener('touchstart', function (e) {
      var touch = e.touches[0];
      if (!touch) return;
      if (touch.clientX > 20) { tracking = false; return; }
      startX    = touch.clientX;
      startY    = touch.clientY;
      startTime = Date.now();
      tracking  = true;
    }, { passive: true });

    document.addEventListener('touchend', function (e) {
      if (!tracking) return;
      tracking = false;
      var touch = e.changedTouches[0];
      if (!touch) return;

      var dx       = touch.clientX - startX;
      var dy       = touch.clientY - startY;
      var elapsed  = Date.now() - startTime || 1;
      var velocity = dx / elapsed;

      /* Only a rightward swipe with enough travel and speed */
      if (dx < 80 || Math.abs(dy) > Math.abs(dx)) return;
      if (velocity < 0.3) return;

      /* Only fire when mobile right-detail is open */
      var shell = window.FS && window.FS.shell;
      if (!shell) return;
      shell.closeDetail();
    }, { passive: true });
  })();

})();
