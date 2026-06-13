/* ==========================================================================
   FieldSight Theme — persist + apply light / dark / auto
   --------------------------------------------------------------------------
   Public API (window.FS.theme):
     init()       — call once at app boot; reads localStorage, applies
                    data-theme, registers prefers-color-scheme listener
     set(mode)    — 'light' | 'dark' | 'auto'; persists + re-applies
     get()        — resolved current mode (auto → 'light' or 'dark')
     getStored()  — raw stored preference ('light' | 'dark' | 'auto')

   Storage key: localStorage['fs.settings.theme']
   ========================================================================== */

(function () {
  'use strict';

  var STORAGE_KEY = 'fs.settings.theme';
  var mql = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)');

  function getStored() {
    try {
      var v = localStorage.getItem(STORAGE_KEY);
      if (v === 'light' || v === 'dark' || v === 'auto') return v;
    } catch (_) {}
    return 'auto';
  }

  function resolvedMode() {
    var stored = getStored();
    if (stored === 'light') return 'light';
    if (stored === 'dark')  return 'dark';
    return (mql && mql.matches) ? 'dark' : 'light';
  }

  function applyDataTheme() {
    document.documentElement.setAttribute('data-theme', resolvedMode());
  }

  function init() {
    applyDataTheme();
    if (mql) {
      mql.addEventListener('change', function () {
        if (getStored() === 'auto') applyDataTheme();
      });
    }
  }

  function set(mode) {
    if (mode !== 'light' && mode !== 'dark' && mode !== 'auto') return;
    try { localStorage.setItem(STORAGE_KEY, mode); } catch (_) {}
    applyDataTheme();
  }

  function get() {
    return resolvedMode();
  }

  if (!window.FS) window.FS = {};
  window.FS.theme = { init: init, set: set, get: get, getStored: getStored };

})();
