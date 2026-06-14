/* ==========================================================================
   FieldSight Density — persist + apply comfortable / compact layout density
   --------------------------------------------------------------------------
   Public API (window.FS.density):
     init()       — call once at app boot; reads localStorage, applies
                    data-density attribute on <html>
     set(mode)    — 'comfortable' | 'compact'; persists + re-applies
     get()        — current stored mode
     getStored()  — raw stored preference (same as get() for density)

   Storage key: localStorage['fs.settings.density']
   Default:     'comfortable' (no data-density attribute on <html>)

   In comfortable mode the attribute is absent so existing CSS values apply
   without change. In compact mode [data-density="compact"] overrides are
   active, tightening row padding, min-height, and list gaps globally.
   ========================================================================== */

(function () {
  'use strict';

  var STORAGE_KEY = 'fs.settings.density';

  function getStored() {
    try {
      var v = localStorage.getItem(STORAGE_KEY);
      if (v === 'comfortable' || v === 'compact') return v;
    } catch (_) {}
    return 'comfortable';
  }

  function applyDataDensity() {
    var mode = getStored();
    if (mode === 'compact') {
      document.documentElement.setAttribute('data-density', 'compact');
    } else {
      document.documentElement.removeAttribute('data-density');
    }
  }

  function init() {
    applyDataDensity();
  }

  function set(mode) {
    if (mode !== 'comfortable' && mode !== 'compact') return;
    try { localStorage.setItem(STORAGE_KEY, mode); } catch (_) {}
    applyDataDensity();
  }

  function get() {
    return getStored();
  }

  if (!window.FS) window.FS = {};
  window.FS.density = { init: init, set: set, get: get, getStored: getStored };

})();
