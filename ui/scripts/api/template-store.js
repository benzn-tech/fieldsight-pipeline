/* ==========================================================================
   FieldSight Template Store — Sprint 10 B.0
   --------------------------------------------------------------------------
   LocalStorage-backed template API. All operations return Promises and
   mirror the real backend surface (PLAN §6 candidate B) so the swap-in
   is a backend-only change with no UI rework.

   Storage key: 'fs_templates_v1'
   Seed:        window.FieldSight.fixtures.templates (templates.fixture.js)

   ADE extraction is simulated: create() stores a stub with
   _status:'extracting', then after ADE_DELAY_MS materialises the
   schema and notifies any registered onExtracted listeners so the
   Library page can re-render without polling.

   Exposed as: window.FS.api.templates  (CRUD)
               window.FS.templateStore  (listener helpers)
   ========================================================================== */

(function () {
  'use strict';

  var STORAGE_KEY  = 'fs_templates_v1';
  var ADE_DELAY_MS = 2200;

  /* ── Helpers ──────────────────────────────────────────────────────────── */

  function clone(obj) { return JSON.parse(JSON.stringify(obj)); }

  function delay(ms) {
    return new Promise(function (resolve) { setTimeout(resolve, ms); });
  }

  function genId(prefix) {
    return prefix + '-' + Date.now() + '-' + Math.random().toString(36).slice(2, 7);
  }

  /* ── Store I/O ─────────────────────────────────────────────────────────── */

  function seedFromFixtures() {
    var fx = window.FieldSight && window.FieldSight.fixtures && window.FieldSight.fixtures.templates;
    if (!fx) return { org: [], personal: [] };
    return { org: (fx.org || []).map(clone), personal: (fx.personal || []).map(clone) };
  }

  function loadStore() {
    try {
      var raw = localStorage.getItem(STORAGE_KEY);
      if (raw) return JSON.parse(raw);
    } catch (_) { /* corrupt — re-seed */ }
    var seeded = seedFromFixtures();
    saveStore(seeded);
    return seeded;
  }

  function saveStore(store) {
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(store)); } catch (_) {}
  }

  function allOf(store) {
    return (store.org || []).concat(store.personal || []);
  }

  function findIn(store, id) {
    return allOf(store).find(function (t) { return t.id === id; }) || null;
  }

  function scopeKey(t) { return t.scope === 'org' ? 'org' : 'personal'; }

  /* ── ADE stub ─────────────────────────────────────────────────────────── */

  var STUB_SCHEMAS = {
    daily: { sections: [
      { title: 'Daily Summary',   kind: 'narrative', fields: [],                                  prompt_hint: 'Key activities and overall progress' },
      { title: 'Workforce',       kind: 'kpi',       fields: ['headcount', 'subcontractors'],     prompt_hint: 'Labour numbers on site' },
      { title: 'Key Decisions',   kind: 'list',      fields: [],                                  prompt_hint: 'Decisions affecting programme or cost' },
      { title: 'Open Actions',    kind: 'table',     fields: ['action', 'owner', 'due_date'],     prompt_hint: 'Outstanding tasks' },
      { title: 'Photos',          kind: 'photos',    fields: [],                                  prompt_hint: 'Site progress photos' },
    ]},
    weekly: { sections: [
      { title: 'Executive Summary',  kind: 'narrative', fields: [],                                        prompt_hint: 'One-paragraph summary for distribution' },
      { title: 'Programme KPIs',     kind: 'kpi',       fields: ['completion_pct', 'days_variance'],       prompt_hint: 'Key metrics vs baseline' },
      { title: 'Completed',          kind: 'list',      fields: [],                                        prompt_hint: 'Tasks completed this week' },
      { title: 'Planned Next Week',  kind: 'list',      fields: [],                                        prompt_hint: 'Tasks for the coming week' },
      { title: 'Issues & Risks',     kind: 'table',     fields: ['issue', 'impact', 'mitigation'],         prompt_hint: 'Open issues' },
    ]},
    monthly: { sections: [
      { title: 'Monthly Summary',   kind: 'narrative', fields: [],                                prompt_hint: 'Month-level summary' },
      { title: 'Progress KPIs',     kind: 'kpi',       fields: ['completion_pct', 'budget_pct'],  prompt_hint: 'Programme and cost metrics' },
      { title: 'Milestones',        kind: 'list',      fields: [],                                prompt_hint: 'Milestones achieved and upcoming' },
      { title: 'Commercial Update', kind: 'narrative', fields: [],                                prompt_hint: 'Cost and variation summary' },
      { title: 'Photos',            kind: 'photos',    fields: [],                                prompt_hint: 'Progress photos' },
    ]},
    incident: { sections: [
      { title: 'Incident Details',   kind: 'kpi',       fields: ['date_time', 'location', 'severity'], prompt_hint: 'Who, what, when, where' },
      { title: 'Description',        kind: 'narrative', fields: [],                                    prompt_hint: 'Factual account' },
      { title: 'Immediate Actions',  kind: 'list',      fields: [],                                    prompt_hint: 'Steps taken immediately' },
      { title: 'Corrective Actions', kind: 'table',     fields: ['action', 'owner', 'due_date'],       prompt_hint: 'Prevention actions' },
      { title: 'Photos & Evidence',  kind: 'photos',    fields: [],                                    prompt_hint: 'Scene photos' },
    ]},
  };

  function stubbedSchema(reportType) {
    return clone(STUB_SCHEMAS[reportType] || STUB_SCHEMAS.daily);
  }

  /* ── Extraction listeners ─────────────────────────────────────────────── */

  var _listeners = [];

  function _notifyExtracted(id) {
    _listeners.forEach(function (fn) { try { fn(id); } catch (_) {} });
  }

  function onExtracted(fn) {
    _listeners.push(fn);
    return function () { _listeners = _listeners.filter(function (f) { return f !== fn; }); };
  }

  /* ── API ──────────────────────────────────────────────────────────────── */

  /* GET /api/templates?scope=org|personal|all */
  function list(scope) {
    return delay(40).then(function () {
      var store = loadStore();
      var rows;
      if (scope === 'org')           rows = store.org      || [];
      else if (scope === 'personal') rows = store.personal || [];
      else                           rows = allOf(store);
      /* Hide soft-deleted entries */
      return { templates: rows.filter(function (t) { return !t._deleted; }).map(clone) };
    });
  }

  /* GET /api/templates/{id} */
  function get(id) {
    return delay(30).then(function () {
      var t = findIn(loadStore(), id);
      if (!t) return Promise.reject({ status: 404, message: 'Template not found' });
      return clone(t);
    });
  }

  /* POST /api/templates — initiates upload + async ADE extraction.
     Returns immediately with _status:'extracting'.
     After ADE_DELAY_MS, schema lands and onExtracted fires. */
  function create(data) {
    var id  = genId('tpl');
    var now = new Date().toISOString();
    var key = (data.scope === 'org') ? 'org' : 'personal';

    var stub = {
      id:            id,
      scope:         data.scope         || 'personal',
      report_type:   data.report_type   || 'daily',
      active:        false,
      owner_user_id: data.owner_user_id || null,
      title:         data.title         || 'New Template',
      description:   data.description   || '',
      created_at:    now,
      _status:       'extracting',
      versions:      [],
    };

    var store = loadStore();
    store[key] = (store[key] || []).concat([stub]);
    saveStore(store);

    /* Simulate ADE completing after a delay */
    delay(ADE_DELAY_MS).then(function () {
      var s2  = loadStore();
      var arr = s2[key] || [];
      var idx = arr.findIndex(function (t) { return t.id === id; });
      if (idx < 0) return;
      var ver = {
        id:                  genId('ver'),
        schema:              stubbedSchema(stub.report_type),
        created_at:          new Date().toISOString(),
        created_by_user_id:  data.owner_user_id || 'system',
        change_note:         'Extracted by ADE (fixture stub)',
      };
      arr[idx] = Object.assign({}, arr[idx], { _status: 'ready', versions: [ver] });
      s2[key]  = arr;
      saveStore(s2);
      _notifyExtracted(id);
    });

    return delay(0).then(function () { return clone(stub); });
  }

  /* PATCH /api/templates/{id}/schema — creates a new immutable version */
  function updateSchema(id, schema, changeNote) {
    return delay(50).then(function () {
      var store = loadStore();
      var t     = findIn(store, id);
      if (!t) return Promise.reject({ status: 404, message: 'Template not found' });
      var caller = (window.AuthMock && window.AuthMock.currentUser) || {};
      var ver = {
        id:                  genId('ver'),
        schema:              clone(schema),
        created_at:          new Date().toISOString(),
        created_by_user_id:  caller.device_id || caller.sub || 'unknown',
        change_note:         changeNote || null,
      };
      var key = scopeKey(t);
      store[key] = store[key].map(function (tmpl) {
        if (tmpl.id !== id) return tmpl;
        return Object.assign({}, tmpl, { versions: tmpl.versions.concat([ver]) });
      });
      saveStore(store);
      return clone(findIn(store, id));
    });
  }

  /* POST /api/templates/{id}/activate — set as default for its scope+report_type */
  function activate(id) {
    return delay(40).then(function () {
      var store = loadStore();
      var t     = findIn(store, id);
      if (!t) return Promise.reject({ status: 404, message: 'Template not found' });
      /* Deactivate same scope+report_type siblings */
      ['org', 'personal'].forEach(function (k) {
        store[k] = (store[k] || []).map(function (tmpl) {
          if (tmpl.id === id) return tmpl;
          if (tmpl.scope === t.scope && tmpl.report_type === t.report_type) {
            return Object.assign({}, tmpl, { active: false });
          }
          return tmpl;
        });
      });
      /* Activate target */
      var key = scopeKey(t);
      store[key] = store[key].map(function (tmpl) {
        if (tmpl.id !== id) return tmpl;
        return Object.assign({}, tmpl, { active: true });
      });
      saveStore(store);
      return clone(findIn(store, id));
    });
  }

  /* DELETE /api/templates/{id} — soft-delete; versions preserved */
  function remove(id) {
    return delay(40).then(function () {
      var store = loadStore();
      var t     = findIn(store, id);
      if (!t) return Promise.reject({ status: 404, message: 'Template not found' });
      var key   = scopeKey(t);
      store[key] = store[key].map(function (tmpl) {
        if (tmpl.id !== id) return tmpl;
        return Object.assign({}, tmpl, { _deleted: true, active: false });
      });
      saveStore(store);
      return { ok: true };
    });
  }

  /* GET /api/templates/{id}/versions */
  function listVersions(id) {
    return delay(30).then(function () {
      var t = findIn(loadStore(), id);
      if (!t) return Promise.reject({ status: 404, message: 'Template not found' });
      return { versions: (t.versions || []).map(clone) };
    });
  }

  /* POST /api/templates/{id}/versions/{vid}/restore */
  function restore(id, vid) {
    return delay(30).then(function () {
      var t   = findIn(loadStore(), id);
      if (!t) return Promise.reject({ status: 404, message: 'Template not found' });
      var ver = (t.versions || []).find(function (v) { return v.id === vid; });
      if (!ver) return Promise.reject({ status: 404, message: 'Version not found' });
      return updateSchema(id, ver.schema, 'Restored from version ' + vid);
    });
  }

  /* GET /api/templates/usage */
  function usageStats() {
    return delay(20).then(function () {
      var store = loadStore();
      var orgCount = (store.org || []).filter(function (t) { return !t._deleted; }).length;
      return {
        org_count:             orgCount,
        personal_count:        (store.personal || []).filter(function (t) { return !t._deleted; }).length,
        ade_calls_this_month:  Math.min(orgCount, 6),
        ade_cap:               50,
      };
    });
  }

  /* ── Favourites ─────────────────────────────────────────────────────────
     Sprint 10 follow-up: Heidi-style favourites row at top of /library.
     Per-user pin list, max 6 entries. localStorage key `fs.lib.favourites`
     (separate from store key so favouriting doesn't clutter the
     template store). Each entry is just a template_id; resolution to
     template happens at render time so a deleted template falls out
     of the row gracefully. */

  var FAV_KEY  = 'fs.lib.favourites';
  var FAV_CAP  = 6;

  function loadFavourites() {
    try {
      var raw = localStorage.getItem(FAV_KEY);
      var arr = raw ? JSON.parse(raw) : [];
      return Array.isArray(arr) ? arr : [];
    } catch (_) { return []; }
  }
  function saveFavourites(arr) {
    try { localStorage.setItem(FAV_KEY, JSON.stringify(arr.slice(0, FAV_CAP))); } catch (_) {}
  }

  function getFavourites() {
    return delay(10).then(function () { return loadFavourites().slice(); });
  }

  function isFavourite(id) {
    return loadFavourites().indexOf(id) >= 0;
  }

  function addFavourite(id) {
    return delay(10).then(function () {
      var favs = loadFavourites();
      if (favs.indexOf(id) < 0 && favs.length < FAV_CAP) {
        favs.push(id);
        saveFavourites(favs);
      }
      return favs;
    });
  }

  function removeFavourite(id) {
    return delay(10).then(function () {
      var favs = loadFavourites().filter(function (x) { return x !== id; });
      saveFavourites(favs);
      return favs;
    });
  }

  function toggleFavourite(id) {
    return isFavourite(id) ? removeFavourite(id) : addFavourite(id);
  }

  /* ── Expose ──────────────────────────────────────────────────────────── */

  if (!window.FS)      window.FS      = {};
  if (!window.FS.api)  window.FS.api  = {};

  window.FS.api.templates = {
    list:          list,
    get:           get,
    create:        create,
    updateSchema:  updateSchema,
    activate:      activate,
    'delete':      remove,
    listVersions:  listVersions,
    restore:       restore,
    usageStats:    usageStats,
    /* Sprint 10 follow-up — favourites */
    getFavourites:    getFavourites,
    isFavourite:      isFavourite,
    addFavourite:     addFavourite,
    removeFavourite:  removeFavourite,
    toggleFavourite:  toggleFavourite,
    FAVOURITES_CAP:   FAV_CAP,
  };

  if (!window.FS.templateStore) window.FS.templateStore = {};
  window.FS.templateStore._notifyExtracted = _notifyExtracted;
  window.FS.templateStore.onExtracted      = onExtracted;

})();
