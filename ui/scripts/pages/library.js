/* ==========================================================================
   FieldSight /library — Sprint 10 B.1 + B.3
   --------------------------------------------------------------------------
   Per-company report template library. Each organisation maintains an
   Org library (admin/gm/director manage) and every user has a Personal
   library for their own formats. The All tab is a read-only union.

   Sprint 10 scope:
     B.0  Stores + permissions (template-store.js / roles.js)
     B.1  This page — route + scaffold + tabs + list + right detail
     B.2  TemplateUploadModal composite (template-upload-modal.js)
     B.3  Skip-edit primary path: source vs extracted schema side-by-side
          + Test render panel + "✓ Use this template" CTA

   Middle column:
     • Tab strip — Org / Personal / All
     • Template list rows: title, report_type badge, active indicator,
       extraction status
     • Permission-gated "+ Upload template" button

   Right detail (B.3 skip-edit primary path):
     • While extracting: spinner + progress note
     • Once ready: 2-col "Source" vs "Extracted schema" review
       + Test-render panel (fills schema sections with sample content)
       + "✓ Use this template" CTA (activates in one click)

   Permission gate: template:manage:self  (all roles — see roles.js B.0)
   Upload button:   template:manage:org   (admin/gm/director for org scope)
                    template:manage:self  (anyone for personal scope)

   Registers as window.FieldSight.PAGES['/library']
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  /* ── Constants ─────────────────────────────────────────────────────── */

  /* Sprint 10 follow-up — tab order changed to All / Organisation /
     Personal per user review. The tab `key` stays 'org' so the
     existing scope filter logic + persisted template.scope === 'org'
     records keep working; only the visible label is "Organisation". */
  var TABS = [
    { key: 'all',      label: 'All'          },
    { key: 'org',      label: 'Organisation' },
    { key: 'personal', label: 'Personal'     },
  ];

  var RT_LABEL = { daily: 'Daily', weekly: 'Weekly', monthly: 'Monthly', incident: 'Incident' };
  var RT_TONE  = { daily: 'info', weekly: 'success', monthly: 'accent', incident: 'danger' };

  var KIND_LABEL = { narrative: 'Narrative', list: 'List', table: 'Table', kpi: 'KPIs', photos: 'Photos' };
  var KIND_ICON  = { narrative: '¶', list: '•', table: '⊞', kpi: '◆', photos: '🖼' };

  /* ── Helpers ───────────────────────────────────────────────────────── */

  function fmtDate(iso) {
    if (!iso) return '';
    var p = iso.slice(0, 10).split('-').map(Number);
    var d = new Date(Date.UTC(p[0], p[1] - 1, p[2]));
    var months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    return d.getUTCDate() + ' ' + months[d.getUTCMonth()] + ' ' + p[0];
  }

  function activeSchema(tpl) {
    if (!tpl || !tpl.versions || !tpl.versions.length) return null;
    var vers = tpl.versions;
    return vers[vers.length - 1].schema;
  }

  /* Sample content for each section kind used in the Test Render panel */
  var SAMPLE = {
    narrative: 'Concrete pours for Grid C foundations completed ahead of schedule. Subcontractor coordination for electrical rough-in confirmed for tomorrow morning. Weather held — no delays.',
    list:      ['Agreed revised sequence for Block B roofing with Coastline Roofing — start Friday', 'Client approved variation VO-14 ($8,400) for additional drainage run', 'Defect list from 2 May inspection signed off by SM'],
    kpi:       { headcount: 24, subcontractors: 3, visitors: 1, completion_pct: '62%', days_variance: '+2', rfi_open: 4, budget_pct: '58%' },
    table:     [
      { action: 'Issue revised IFC drawings for Block B roof', owner: 'James Lamb',    due_date: '12 May 2026' },
      { action: 'Confirm crane availability w/c 18 May',        owner: 'Jarley Trainor', due_date: '10 May 2026' },
      { action: 'Submit VO-15 for approval',                    owner: 'James Lamb',    due_date: '15 May 2026' },
    ],
    photos:    ['Progress photo 1', 'Progress photo 2', 'Progress photo 3'],
  };

  /* ── Context ───────────────────────────────────────────────────────── */

  var LibraryContext = React.createContext(null);

  /* ── Provider ──────────────────────────────────────────────────────── */

  function LibraryProvider(props) {
    var caller    = (window.AuthMock && window.AuthMock.currentUser) || {};
    var canManageOrg = window.FS && window.FS.can && window.FS.can(caller, 'template:manage:org');

    var tabRef    = React.useState('all');   /* Sprint 10 follow-up — default 'all' */
    var tab       = tabRef[0]; var setTab = tabRef[1];

    /* Sprint 10 follow-up — favourites pin list (per-user, max 6). */
    var favRef    = React.useState([]);
    var favIds    = favRef[0]; var setFavIds = favRef[1];

    var stateRef  = React.useState({ status: 'loading', rows: [] });
    var state     = stateRef[0]; var setState = stateRef[1];

    var selRef    = React.useState(null);
    var sel       = selRef[0]; var setSel = selRef[1];

    var uploadRef = React.useState(null);  /* null | 'org' | 'personal' */
    var uploadFor = uploadRef[0]; var setUploadFor = uploadRef[1];

    var retryRef  = React.useState(0);
    var retry     = retryRef[0]; var setRetry = retryRef[1];

    function load() {
      setState(function (s) { return Object.assign({}, s, { status: 'loading' }); });
      var scope = (tab === 'org' || tab === 'personal') ? tab : 'all';
      window.FS.api.templates.list(scope === 'all' ? undefined : scope).then(function (res) {
        setState({ status: 'ok', rows: res.templates || [] });
      }).catch(function (err) {
        setState({ status: 'error', rows: [], error: (err && err.message) || 'Could not load templates.' });
      });
    }

    /* Initial load + re-load when tab or retry changes */
    React.useEffect(function () { load(); }, [tab, retry]);

    /* Sprint 10 follow-up — load favourites once + on retry */
    React.useEffect(function () {
      if (!window.FS.api.templates.getFavourites) return;
      window.FS.api.templates.getFavourites().then(function (ids) {
        setFavIds(ids || []);
      });
    }, [retry]);

    function toggleFavourite(id) {
      window.FS.api.templates.toggleFavourite(id).then(function (next) {
        setFavIds(next || []);
        if (window.FS && window.FS.toast) {
          var nowFav = (next || []).indexOf(id) >= 0;
          window.FS.toast.show({
            message: nowFav ? 'Added to Favourites' : 'Removed from Favourites',
            tone:    nowFav ? 'success' : 'info',
          });
        }
      }).catch(function () {
        if (window.FS && window.FS.toast) {
          window.FS.toast.show({ message: 'Favourites cap reached (6)', tone: 'warning' });
        }
      });
    }

    /* Listen for ADE extraction completions to auto-refresh */
    React.useEffect(function () {
      if (!window.FS.templateStore) return;
      var unsub = window.FS.templateStore.onExtracted(function () {
        /* Small delay so the store write settles before we re-read */
        setTimeout(function () { setRetry(function (n) { return n + 1; }); }, 100);
      });
      return unsub;
    }, []);

    function handleUploadComplete(stub) {
      setUploadFor(null);
      setRetry(function (n) { return n + 1; });
      setSel(stub);
      if (window.FS && window.FS.toast) {
        window.FS.toast.show({ message: 'Template uploaded — extracting schema…', tone: 'info' });
      }
    }

    function handleActivate(tpl) {
      window.FS.api.templates.activate(tpl.id).then(function (updated) {
        setRetry(function (n) { return n + 1; });
        setSel(updated);
        if (window.FS && window.FS.toast) {
          window.FS.toast.show({ message: '"' + updated.title + '" set as active template', tone: 'success' });
        }
      }).catch(function () {
        if (window.FS && window.FS.toast) window.FS.toast.show({ message: 'Could not activate template', tone: 'error' });
      });
    }

    function reload() { setRetry(function (n) { return n + 1; }); }

    return React.createElement(LibraryContext.Provider, {
      value: {
        caller, canManageOrg, tab, setTab, state, sel, setSel,
        uploadFor, setUploadFor, handleUploadComplete, handleActivate, reload,
        /* Sprint 10 follow-up — favourites */
        favIds, toggleFavourite,
      },
    }, props.children);
  }

  /* ── Middle column ─────────────────────────────────────────────────── */

  function LibraryMiddle() {
    var ctx = React.useContext(LibraryContext);

    if (!ctx) return null;

    var tab          = ctx.tab;
    var setTab       = ctx.setTab;
    var state        = ctx.state;
    var sel          = ctx.sel;
    var setSel       = ctx.setSel;
    var canManageOrg = ctx.canManageOrg;
    var setUploadFor = ctx.setUploadFor;
    var caller       = ctx.caller;

    var Badge  = window.FieldSight.Badge;
    var Button = window.FieldSight.Button;
    var ErrorBanner = window.FieldSight.ErrorBanner;

    /* Can user upload to the current tab's scope? */
    var canUploadHere = tab === 'personal'
      ? !!(window.FS && window.FS.can && window.FS.can(caller, 'template:manage:self'))
      : (tab === 'org' && canManageOrg) || (tab === 'all' && canManageOrg);

    /* Group rows by report_type for the list */
    var rows = state.rows || [];

    return React.createElement('div', { className: 'fs-library__middle' },

      /* Header */
      React.createElement('div', { className: 'fs-library__header' },
        React.createElement('h1', { className: 'fs-library__title' }, 'Template Library'),
        canUploadHere && React.createElement(Button, {
          variant:  'primary',
          size:     'sm',
          onClick:  function () { setUploadFor(tab === 'personal' ? 'personal' : 'org'); },
        }, '+ Upload template'),
      ),

      /* Tab strip */
      React.createElement('div', { className: 'fs-library__tabs', role: 'tablist' },
        TABS.map(function (t) {
          return React.createElement('button', {
            key:           t.key,
            role:          'tab',
            'aria-selected': tab === t.key,
            className:     'fs-library__tab' + (tab === t.key ? ' fs-library__tab--active' : ''),
            onClick:       function () { setTab(t.key); setSel(null); },
          }, t.label);
        }),
      ),

      /* Sprint 10 follow-up — Favourites row (Heidi-style pin shelf) */
      React.createElement(FavouritesRow, { ctx: ctx }),

      /* Body */
      state.status === 'loading' && React.createElement('div', { className: 'fs-library__loading' },
        React.createElement('div', { className: 'fs-library__skeleton' }),
        React.createElement('div', { className: 'fs-library__skeleton' }),
        React.createElement('div', { className: 'fs-library__skeleton' }),
      ),

      state.status === 'error' && ErrorBanner
        ? React.createElement(ErrorBanner, { message: state.error || 'Failed to load templates.' })
        : null,

      state.status === 'ok' && rows.length === 0 && React.createElement('div', { className: 'fs-library__empty' },
        React.createElement('p', null, tab === 'personal'
          ? 'No personal templates yet. Upload one to get started.'
          : 'No org templates yet.' + (canManageOrg ? ' Upload one to make it available to all users.' : '')
        ),
      ),

      state.status === 'ok' && rows.length > 0 && React.createElement('div', { className: 'fs-library__list', role: 'list' },
        rows.map(function (tpl) {
          var isSelected   = sel && sel.id === tpl.id;
          var isExtracting = tpl._status === 'extracting';
          var hasSchema    = tpl.versions && tpl.versions.length > 0;
          var isFav        = (ctx.favIds || []).indexOf(tpl.id) >= 0;

          return React.createElement('div', {
            key:          tpl.id,
            role:         'listitem',
            className:    'fs-library__row' + (isSelected ? ' fs-library__row--selected' : ''),
            onClick:      function () { setSel(tpl); },
            tabIndex:     0,
            onKeyDown:    function (e) { if (e.key === 'Enter' || e.key === ' ') setSel(tpl); },
            'aria-current': isSelected ? 'true' : undefined,
          },
            React.createElement('div', { className: 'fs-library__row-main' },
              React.createElement('span', { className: 'fs-library__row-title' }, tpl.title),
              tpl.active && React.createElement('span', { className: 'fs-library__active-badge' }, 'Active'),
            ),
            React.createElement('div', { className: 'fs-library__row-meta' },
              Badge && React.createElement(Badge, {
                tone:  RT_TONE[tpl.report_type] || 'neutral',
                label: RT_LABEL[tpl.report_type] || tpl.report_type,
                size:  'xs',
              }),
              tpl.scope === 'personal' && React.createElement('span', { className: 'fs-library__personal-tag' }, 'Personal'),
              isExtracting && React.createElement('span', { className: 'fs-library__extracting-tag' }, 'Extracting…'),
              !isExtracting && hasSchema && React.createElement('span', { className: 'fs-library__sections-count' },
                activeSchema(tpl) ? activeSchema(tpl).sections.length + ' sections' : '',
              ),
            ),
            /* Sprint 10 follow-up — favourite toggle (right-aligned star). */
            React.createElement('button', {
              type:        'button',
              className:   'fs-library__row-fav' + (isFav ? ' fs-library__row-fav--on' : ''),
              onClick:     function (e) { e.stopPropagation(); ctx.toggleFavourite(tpl.id); },
              'aria-label': isFav ? 'Remove from favourites' : 'Add to favourites',
              'aria-pressed': isFav,
              title:       isFav ? 'Remove from favourites' : 'Add to favourites',
            }, isFav ? '★' : '☆'),
          );
        }),
      ),

      /* Upload modal */
      ctx.uploadFor && window.FieldSight.TemplateUploadModal && React.createElement(
        window.FieldSight.TemplateUploadModal,
        {
          scope:      ctx.uploadFor,
          onComplete: ctx.handleUploadComplete,
          onCancel:   function () { ctx.setUploadFor(null); },
        },
      ),

    );
  }

  /* ── Sprint 10 follow-up · Favourites row (Heidi-style pin shelf) ───── */

  function FavouritesRow(props) {
    var ctx = props.ctx;
    if (!ctx) return null;
    var Badge = window.FieldSight.Badge;
    var FAV_CAP = (window.FS.api.templates && window.FS.api.templates.FAVOURITES_CAP) || 6;

    /* Resolve favourite IDs against the loaded rows; templates not in
       the current tab still show (we union across scopes) so the pin
       shelf is stable as the user toggles between Org / Personal. */
    var allRows = ctx.state && ctx.state.rows ? ctx.state.rows : [];
    var pinned  = (ctx.favIds || [])
      .map(function (id) {
        return allRows.filter(function (r) { return r.id === id; })[0];
      })
      .filter(Boolean);

    /* Hide the row entirely on the All tab when nothing is favourited
       — feels less empty than showing 6 dotted slots before the user
       has started using the feature. */
    if (pinned.length === 0 && (ctx.favIds || []).length === 0) {
      /* Show one "empty hint" tile so the feature is discoverable. */
      return React.createElement('div', { className: 'fs-library__favs-row fs-library__favs-row--empty' },
        React.createElement('div', { className: 'fs-library__favs-label' }, 'Favourites'),
        React.createElement('div', { className: 'fs-library__favs-tiles' },
          React.createElement('div', { className: 'fs-library__favs-empty-hint' },
            'Tap the ☆ on any template to pin it here for one-click access.'
          ),
        ),
      );
    }

    var emptySlots = Math.max(0, FAV_CAP - pinned.length);

    return React.createElement('div', { className: 'fs-library__favs-row' },
      React.createElement('div', { className: 'fs-library__favs-label' }, 'Favourites'),
      React.createElement('div', { className: 'fs-library__favs-tiles' },
        pinned.map(function (tpl) {
          return React.createElement('button', {
            key:        tpl.id,
            type:       'button',
            className:  'fs-library__fav-tile',
            onClick:    function () { ctx.setSel(tpl); },
            title:      tpl.title + ' · ' + (RT_LABEL[tpl.report_type] || tpl.report_type),
          },
            React.createElement('span', { className: 'fs-library__fav-tile-name' },
              tpl.title),
            React.createElement('span', { className: 'fs-library__fav-tile-meta' },
              Badge && React.createElement(Badge, {
                tone:  RT_TONE[tpl.report_type] || 'neutral',
                label: RT_LABEL[tpl.report_type] || tpl.report_type,
                size:  'xs',
              }),
              tpl.scope === 'personal'
                ? React.createElement('span', { className: 'fs-library__personal-tag' }, 'Personal')
                : null,
            ),
            React.createElement('span', {
              className: 'fs-library__fav-tile-unpin',
              onClick:   function (e) { e.stopPropagation(); ctx.toggleFavourite(tpl.id); },
              role:      'button',
              tabIndex:  0,
              onKeyDown: function (e) {
                if (e.key === 'Enter' || e.key === ' ') { e.stopPropagation(); ctx.toggleFavourite(tpl.id); }
              },
              title:    'Remove from favourites',
              'aria-label': 'Remove from favourites',
            }, '×'),
          );
        }),
        /* Empty slots — visual rhythm + invitation to add more */
        Array.from({ length: emptySlots }).map(function (_, i) {
          return React.createElement('div', {
            key:       'empty-' + i,
            className: 'fs-library__fav-tile fs-library__fav-tile--empty',
            'aria-hidden': true,
          }, '+');
        }),
      ),
    );
  }

  /* ── B.4 Schema Editor (rename / reorder / delete) ─────────────────── */

  function SchemaEditor(props) {
    var templateId = props.templateId;
    var schema     = props.schema;
    var Button     = window.FieldSight && window.FieldSight.Button;

    /* Sprint 10 follow-up — sections now support 1-level nesting via
       a `children: []` array. Init preserves any existing children;
       backwards-compat with flat schemas (children defaults to []). */
    var secRef     = React.useState(function () {
      var counter = { n: 0 };
      function tagKeys(arr) {
        return (arr || []).map(function (s) {
          return {
            title:       s.title,
            kind:        s.kind,
            fields:      s.fields || [],
            prompt_hint: s.prompt_hint || '',
            children:    tagKeys(s.children || []),
            _key:        counter.n++,
          };
        });
      }
      return tagKeys(schema.sections || []);
    });
    var sections    = secRef[0]; var setSections = secRef[1];

    var noteRef     = React.useState('');
    var changeNote  = noteRef[0]; var setChangeNote = noteRef[1];

    var savingRef   = React.useState(false);
    var saving      = savingRef[0]; var setSaving = savingRef[1];

    var errRef      = React.useState(null);
    var saveErr     = errRef[0]; var setSaveErr = errRef[1];

    /* Drag state — current dragged path + current hover target +
       drop zone within target ('before' | 'into' | 'after'). */
    var dragRef    = React.useState(null);
    var dragPath   = dragRef[0]; var setDragPath = dragRef[1];

    var hoverRef   = React.useState(null);
    var hoverPath  = hoverRef[0]; var setHoverPath = hoverRef[1];

    var zoneRef    = React.useState('after');
    var dropZone   = zoneRef[0]; var setDropZone = zoneRef[1];

    /* ── Path helpers (path = 'i' or 'i.j') ──────────────────────────── */

    function pathToArr(p)      { return p.split('.').map(Number); }
    function pathEq(a, b)      { return a === b; }
    function pathStartsWith(child, parent) {
      return child === parent || child.indexOf(parent + '.') === 0;
    }
    function getAt(arr, p) {
      var idx = pathToArr(p);
      var cur = arr;
      var node = null;
      for (var i = 0; i < idx.length; i++) {
        node = cur[idx[i]];
        if (!node) return null;
        cur = node.children || [];
      }
      return node;
    }
    function removeAt(arr, p) {
      var idx = pathToArr(p);
      function rec(list, i) {
        var copy = list.slice();
        if (i === idx.length - 1) {
          copy.splice(idx[i], 1);
          return copy;
        }
        copy[idx[i]] = Object.assign({}, copy[idx[i]], {
          children: rec(copy[idx[i]].children || [], i + 1),
        });
        return copy;
      }
      return rec(arr, 0);
    }
    function insertAt(arr, p, position, item) {
      /* position: 'before' | 'after' | 'into' (last child of node at p) */
      var idx = pathToArr(p);
      function rec(list, i) {
        var copy = list.slice();
        if (i === idx.length - 1) {
          if (position === 'before')      copy.splice(idx[i], 0, item);
          else if (position === 'after')  copy.splice(idx[i] + 1, 0, item);
          else if (position === 'into') {
            /* Append into the target's children. Only allow nesting
               at top level (don't make grand-children → keeps 1-level
               cap) — if target is already a child, treat as 'after'. */
            if (i > 0) {
              copy.splice(idx[i] + 1, 0, item);
            } else {
              var withChildren = Object.assign({}, copy[idx[i]], {
                children: (copy[idx[i]].children || []).concat([item]),
              });
              copy[idx[i]] = withChildren;
            }
          }
          return copy;
        }
        copy[idx[i]] = Object.assign({}, copy[idx[i]], {
          children: rec(copy[idx[i]].children || [], i + 1),
        });
        return copy;
      }
      return rec(arr, 0);
    }

    /* ── Mutations ───────────────────────────────────────────────────── */

    function rename(p, val) {
      setSections(function (prev) {
        var idx = pathToArr(p);
        function rec(list, i) {
          var copy = list.slice();
          if (i === idx.length - 1) {
            copy[idx[i]] = Object.assign({}, copy[idx[i]], { title: val });
            return copy;
          }
          copy[idx[i]] = Object.assign({}, copy[idx[i]], {
            children: rec(copy[idx[i]].children || [], i + 1),
          });
          return copy;
        }
        return rec(prev, 0);
      });
    }

    function del(p) {
      setSections(function (prev) {
        /* Promote children to the deleted node's level rather than
           dropping them — gives the user a recoverable result if they
           delete a parent by accident. */
        var node = getAt(prev, p);
        var children = (node && node.children) || [];
        var without  = removeAt(prev, p);
        if (children.length === 0) return without;
        /* Insert children at deleted parent's position. */
        var parts = pathToArr(p);
        if (parts.length === 1) {
          /* Top-level delete — splice children at idx */
          var copy = without.slice();
          copy.splice.apply(copy, [parts[0], 0].concat(children.map(function (c) {
            return Object.assign({}, c, { children: [] });
          })));
          return copy;
        }
        return without;
      });
    }

    function moveByDrag() {
      if (!dragPath || !hoverPath || dragPath === hoverPath) return;
      if (pathStartsWith(hoverPath, dragPath)) return;  /* prevent dropping onto self/descendant */
      setSections(function (prev) {
        var node = getAt(prev, dragPath);
        if (!node) return prev;
        /* 1-level nesting cap — if the dragged node has children and
           we're trying to nest it, drop those children at top level
           rather than silently turning them into invisible
           grand-children. */
        var effectiveZone = dropZone;
        if (effectiveZone === 'into' && node.children && node.children.length > 0) {
          effectiveZone = 'after';  /* refuse the nest, fall back to reorder */
        }
        var without  = removeAt(prev, dragPath);
        /* Recompute hover path after removal — if drop target's path
           drifts because the source was removed earlier in the tree.
           Simpler safe approach: re-insert relative to the original
           target by id, not path. Since _key is unique, find it: */
        function findPath(list, key, base) {
          for (var i = 0; i < list.length; i++) {
            var p = base ? base + '.' + i : '' + i;
            if (list[i]._key === key) return p;
            var inChild = findPath(list[i].children || [], key, p);
            if (inChild) return inChild;
          }
          return null;
        }
        var target = getAt(prev, hoverPath);
        var newHover = target ? findPath(without, target._key, '') : null;
        if (!newHover) return prev;
        return insertAt(without, newHover, effectiveZone, node);
      });
    }

    function save() {
      setSaving(true); setSaveErr(null);
      function strip(arr) {
        return arr.map(function (s) {
          var out = { title: s.title, kind: s.kind, fields: s.fields, prompt_hint: s.prompt_hint };
          if (s.children && s.children.length) out.children = strip(s.children);
          return out;
        });
      }
      var newSchema = { sections: strip(sections) };
      window.FS.api.templates.updateSchema(templateId, newSchema, changeNote || 'Edited sections').then(function (updated) {
        setSaving(false);
        if (props.onSaved) props.onSaved(updated);
      }).catch(function (err) {
        setSaving(false);
        setSaveErr((err && err.message) || 'Could not save');
      });
    }

    /* ── Drag handlers ───────────────────────────────────────────────── */

    function onDragStart(p) {
      return function (e) {
        /* CRITICAL: <li>s are nested (parent contains child <ol> with
           more <li>s). Without stopPropagation, dragging a child fires
           the child's onDragStart, then bubbles up and the parent's
           handler overwrites dragPath to the parent's path — moving
           the parent (and all its children) instead of the child. The
           visible effect is "the parent disappears" because it gets
           folded as a child of the drop target. */
        e.stopPropagation();
        e.dataTransfer.effectAllowed = 'move';
        e.dataTransfer.setData('text/plain', p);
        setDragPath(p);
      };
    }
    function onDragOver(p) {
      return function (e) {
        e.preventDefault();
        e.stopPropagation();  /* same nesting reason as onDragStart */
        e.dataTransfer.dropEffect = 'move';
        if (!dragPath) return;
        if (pathStartsWith(p, dragPath)) return;  /* skip self/descendants */
        var rect = e.currentTarget.getBoundingClientRect();
        var y    = e.clientY - rect.top;
        var h    = rect.height;
        var isChild = pathToArr(p).length > 1;
        /* On child targets, never compute 'into' (1-level nesting cap).
           On top-level targets, tighten 'into' to the middle 20% so
           reorder is the easy gesture and nest is deliberate. */
        var zone;
        if (isChild) {
          zone = (y < h * 0.5) ? 'before' : 'after';
        } else if (y < h * 0.40) {
          zone = 'before';
        } else if (y > h * 0.60) {
          zone = 'after';
        } else {
          zone = 'into';
        }
        setHoverPath(p);
        setDropZone(zone);
      };
    }
    function onDrop() {
      return function (e) {
        e.preventDefault();
        e.stopPropagation();
        moveByDrag();
        setDragPath(null); setHoverPath(null); setDropZone('after');
      };
    }
    function onDragEnd() {
      setDragPath(null); setHoverPath(null); setDropZone('after');
    }

    /* Promote a child back to top level — explicit escape hatch since
       drag-back-to-top is fiddly inside nested <ol>. Inserts the
       promoted node just after its current parent. */
    function promote(p) {
      var parts = pathToArr(p);
      if (parts.length < 2) return;  /* already top-level */
      setSections(function (prev) {
        var node = getAt(prev, p);
        if (!node) return prev;
        var without = removeAt(prev, p);
        var parentIdx = parts[0];
        var copy = without.slice();
        copy.splice(parentIdx + 1, 0, Object.assign({}, node, { children: [] }));
        return copy;
      });
    }

    /* ── Render helpers ──────────────────────────────────────────────── */

    function renderSectionRow(sec, p, isChild, totalAtLevel) {
      var idx           = pathToArr(p)[pathToArr(p).length - 1];
      var hovered       = hoverPath === p && dragPath !== null && !pathStartsWith(p, dragPath);
      var hoveredZone   = hovered ? dropZone : null;

      return React.createElement('li', {
        key:       sec._key,
        className: 'fs-library__editor-section'
                   + (isChild ? ' fs-library__editor-section--child' : '')
                   + (dragPath === p ? ' fs-library__editor-section--dragging' : '')
                   + (hoveredZone === 'before' ? ' fs-library__editor-section--drop-before' : '')
                   + (hoveredZone === 'into'   ? ' fs-library__editor-section--drop-into'   : '')
                   + (hoveredZone === 'after'  ? ' fs-library__editor-section--drop-after'  : ''),
        draggable: true,
        onDragStart: onDragStart(p),
        onDragOver:  onDragOver(p),
        onDrop:      onDrop(),
        onDragEnd:   onDragEnd,
      },
        React.createElement('span', {
          className:   'fs-library__editor-drag-handle',
          'aria-hidden': true,
          title:       'Drag to reorder or nest',
        }, '⋮⋮'),
        React.createElement('span', {
          className: 'fs-library__editor-kind-tag',
          title:     KIND_LABEL[sec.kind] || sec.kind,
        }, KIND_ICON[sec.kind] || '•'),
        React.createElement('input', {
          className:   'fs-library__editor-section-input',
          value:       sec.title,
          onChange:    function (e) { rename(p, e.target.value); },
          'aria-label': 'Section title',
          maxLength:   80,
        }),
        React.createElement('div', { className: 'fs-library__editor-section-controls' },
          isChild && React.createElement('button', {
            type: 'button', className: 'fs-library__editor-promote-btn',
            onClick: function () { promote(p); },
            'aria-label': 'Promote to top level', title: 'Promote to top level',
          }, '↤'),
          React.createElement('button', {
            type: 'button', className: 'fs-library__editor-delete-btn',
            onClick: function () { del(p); }, disabled: sections.length <= 1 && !isChild,
            'aria-label': 'Delete section', title: 'Delete',
          }, '×'),
        ),
        /* Recursive children (only top-level can have children — 1-level cap) */
        !isChild && sec.children && sec.children.length > 0
          ? React.createElement('ol', { className: 'fs-library__editor-section-list fs-library__editor-children' },
              sec.children.map(function (child, j) {
                return renderSectionRow(child, p + '.' + j, true, sec.children.length);
              }),
            )
          : null,
      );
    }

    return React.createElement('div', { className: 'fs-library__editor' },

      React.createElement('p', { className: 'fs-library__editor-hint' },
        'Drag the ⋮⋮ handle to reorder. Drop a section ',
        React.createElement('strong', null, 'into the middle'),
        ' of another to make it a sub-section. Use ',
        React.createElement('strong', null, '↤'),
        ' on a sub-section to promote it back to top level. Re-upload to change a section\'s kind.'
      ),

      React.createElement('ol', { className: 'fs-library__editor-section-list' },
        sections.map(function (sec, idx) {
          return renderSectionRow(sec, '' + idx, false, sections.length);
        }),
      ),

      React.createElement('div', { className: 'fs-library__editor-footer' },
        React.createElement('label', { className: 'fs-library__editor-change-label' }, 'Change note'),
        React.createElement('input', {
          className:   'fs-library__editor-change-input',
          type:        'text',
          placeholder: 'e.g. Removed photos section',
          value:       changeNote,
          onChange:    function (e) { setChangeNote(e.target.value); },
          maxLength:   200,
        }),
        saveErr && React.createElement('p', { className: 'fs-library__editor-error' }, saveErr),
        React.createElement('div', { className: 'fs-library__editor-actions' },
          Button && React.createElement(Button, {
            variant: 'primary', size: 'sm',
            onClick: save,
            disabled: saving || sections.length === 0,
          }, saving ? 'Saving…' : 'Save changes'),
          Button && React.createElement(Button, {
            variant: 'ghost', size: 'sm',
            onClick: props.onCancel,
          }, 'Cancel'),
        ),
      ),

    );
  }

  /* ── B.5 Version History Panel ─────────────────────────────────────── */

  function VersionHistoryPanel(props) {
    var templateId   = props.templateId;
    var latestSchema = props.latestSchema;
    var canManage    = props.canManage;
    var Button       = window.FieldSight && window.FieldSight.Button;

    var loadRef      = React.useState({ status: 'loading', versions: [] });
    var load         = loadRef[0]; var setLoad = loadRef[1];

    var selVidRef    = React.useState(null);
    var selVid       = selVidRef[0]; var setSelVid = selVidRef[1];

    var restoringRef = React.useState(false);
    var restoring    = restoringRef[0]; var setRestoring = restoringRef[1];

    React.useEffect(function () {
      setLoad({ status: 'loading', versions: [] });
      window.FS.api.templates.listVersions(templateId).then(function (res) {
        /* Display newest-first */
        setLoad({ status: 'ok', versions: (res.versions || []).slice().reverse() });
      }).catch(function () {
        setLoad({ status: 'error', versions: [] });
      });
    }, [templateId]);

    function diffSections(verSchema) {
      if (!verSchema || !latestSchema) return null;
      var vTitles = (verSchema.sections || []).map(function (s) { return s.title; });
      var lTitles = (latestSchema.sections || []).map(function (s) { return s.title; });
      return {
        same:    vTitles.filter(function (t) { return lTitles.indexOf(t) !== -1; }),
        removed: vTitles.filter(function (t) { return lTitles.indexOf(t) === -1; }),
        added:   lTitles.filter(function (t) { return vTitles.indexOf(t) === -1; }),
      };
    }

    function restore(vid) {
      setRestoring(true);
      window.FS.api.templates.restore(templateId, vid).then(function (updated) {
        setRestoring(false);
        if (window.FS && window.FS.toast) window.FS.toast.show({ message: 'Restored as a new version', tone: 'success' });
        if (props.onRestored) props.onRestored(updated);
      }).catch(function (err) {
        setRestoring(false);
        if (window.FS && window.FS.toast) window.FS.toast.show({ message: (err && err.message) || 'Restore failed', tone: 'error' });
      });
    }

    if (load.status === 'loading') {
      return React.createElement('div', { className: 'fs-library__history' },
        React.createElement('p', { style: { color: 'var(--text-tertiary)', fontSize: '13px', padding: '12px 0' } }, 'Loading…'),
      );
    }

    if (load.status === 'error') {
      return React.createElement('div', { className: 'fs-library__history' },
        React.createElement('p', { style: { color: 'var(--text-danger)', fontSize: '13px', padding: '12px 0' } }, 'Could not load version history.'),
      );
    }

    var versions = load.versions;

    return React.createElement('div', { className: 'fs-library__history' },

      React.createElement('p', { className: 'fs-library__history-intro' },
        versions.length + ' version' + (versions.length === 1 ? '' : 's') + '. Old versions are read-only — restore creates a new version.'
      ),

      React.createElement('div', { className: 'fs-library__history-list' },
        versions.map(function (ver, idx) {
          var isSelected = ver.id === selVid;
          var isLatest   = idx === 0;
          var diff       = isSelected ? diffSections(ver.schema) : null;

          return React.createElement('div', {
            key:       ver.id,
            className: 'fs-library__history-ver' + (isSelected ? ' fs-library__history-ver--selected' : ''),
            onClick:   function () { setSelVid(isSelected ? null : ver.id); },
            tabIndex:  0,
            onKeyDown: function (e) { if (e.key === 'Enter' || e.key === ' ') setSelVid(isSelected ? null : ver.id); },
          },
            React.createElement('div', { className: 'fs-library__history-ver-header' },
              React.createElement('span', { className: 'fs-library__history-ver-date' }, fmtDate(ver.created_at)),
              isLatest && React.createElement('span', { className: 'fs-library__history-ver-badge' }, 'Current'),
            ),
            React.createElement('div', { className: 'fs-library__history-ver-meta' },
              'By ' + (ver.created_by_user_id === 'system' ? 'FieldSight AI' : ver.created_by_user_id),
            ),
            ver.change_note && React.createElement('div', { className: 'fs-library__history-ver-note' }, ver.change_note),

            /* Diff panel — expanded on click */
            isSelected && diff && (diff.same.length || diff.removed.length || diff.added.length)
              ? React.createElement('div', { className: 'fs-library__history-diff' },
                  diff.same.map(function (t) {
                    return React.createElement('div', { key: t, className: 'fs-library__history-diff-row' }, '= ' + t);
                  }),
                  diff.removed.map(function (t) {
                    return React.createElement('div', { key: 'rem-' + t, className: 'fs-library__history-diff-row fs-library__history-diff-removed' }, '− ' + t);
                  }),
                  diff.added.map(function (t) {
                    return React.createElement('div', { key: 'add-' + t, className: 'fs-library__history-diff-row fs-library__history-diff-added' }, '+ ' + t + ' (added later)');
                  }),
                )
              : null,

            /* Restore button */
            isSelected && !isLatest && canManage && Button
              ? React.createElement('div', { className: 'fs-library__history-ver-actions' },
                  React.createElement(Button, {
                    variant: 'secondary', size: 'sm',
                    onClick: function (e) { e.stopPropagation(); restore(ver.id); },
                    disabled: restoring,
                  }, restoring ? 'Restoring…' : 'Restore as new version'),
                )
              : null,
          );
        }),
      ),

    );
  }

  /* ── Right detail (B.1/B.3/B.4/B.5) ────────────────────────────────── */

  function LibraryRight() {
    var ctx = React.useContext(LibraryContext);

    /* All hooks unconditionally before any early return */
    var viewRef = React.useState('preview');
    var view    = viewRef[0]; var setView = viewRef[1];

    var selId = ctx && ctx.sel ? ctx.sel.id : null;
    React.useEffect(function () { setView('preview'); }, [selId]);

    if (!ctx) return null;

    var sel            = ctx.sel;
    var handleActivate = ctx.handleActivate;
    var canManageOrg   = ctx.canManageOrg;
    var caller         = ctx.caller;
    var Badge          = window.FieldSight.Badge;
    var Button         = window.FieldSight.Button;

    /* Empty state */
    if (!sel) {
      return React.createElement('div', { className: 'fs-library__right fs-library__right--empty' },
        React.createElement('p', { className: 'fs-library__right-hint' }, 'Select a template to preview it.'),
      );
    }

    var schema       = activeSchema(sel);
    var ver          = sel.versions && sel.versions.length ? sel.versions[sel.versions.length - 1] : null;
    var isExtracting = sel._status === 'extracting';

    var canActivate = sel.scope === 'org'
      ? canManageOrg
      : !!(window.FS && window.FS.can && window.FS.can(caller, 'template:manage:self'));

    /* ── Extracting state ── */
    if (isExtracting) {
      return React.createElement('div', { className: 'fs-library__right' },
        React.createElement('div', { className: 'fs-library__right-header' },
          React.createElement('h2', { className: 'fs-library__right-title' }, sel.title),
          Badge && React.createElement(Badge, { tone: RT_TONE[sel.report_type] || 'neutral', label: RT_LABEL[sel.report_type] || sel.report_type }),
        ),
        React.createElement('div', { className: 'fs-library__extracting' },
          React.createElement('div', { className: 'fs-library__extracting-spinner' }),
          React.createElement('p', { className: 'fs-library__extracting-label' }, 'AI is extracting the template schema…'),
          React.createElement('p', { className: 'fs-library__extracting-sub' }, 'This usually takes a few seconds. The page will update automatically.'),
        ),
      );
    }

    /* ── No schema yet ── */
    if (!schema) {
      return React.createElement('div', { className: 'fs-library__right' },
        React.createElement('div', { className: 'fs-library__right-header' },
          React.createElement('h2', { className: 'fs-library__right-title' }, sel.title),
        ),
        React.createElement('p', { style: { color: 'var(--text-secondary)', padding: '16px' } }, 'No schema available yet.'),
      );
    }

    /* ── Main: Preview / Edit / History ── */
    return React.createElement('div', { className: 'fs-library__right' },

      /* Header */
      React.createElement('div', { className: 'fs-library__right-header' },
        React.createElement('div', { className: 'fs-library__right-title-row' },
          React.createElement('h2', { className: 'fs-library__right-title' }, sel.title),
          sel.active && React.createElement('span', { className: 'fs-library__active-badge' }, 'Active'),
        ),
        React.createElement('div', { className: 'fs-library__right-meta' },
          Badge && React.createElement(Badge, { tone: RT_TONE[sel.report_type] || 'neutral', label: RT_LABEL[sel.report_type] || sel.report_type }),
          React.createElement('span', { className: 'fs-library__scope-tag' }, sel.scope === 'org' ? 'Org' : 'Personal'),
        ),
        sel.description && React.createElement('p', { className: 'fs-library__right-desc' }, sel.description),
        ver && React.createElement('p', { className: 'fs-library__right-version-note' },
          'Version ' + sel.versions.length + ' · updated ' + fmtDate(ver.created_at),
        ),
      ),

      /* Sub-nav: Preview / Edit / History */
      React.createElement('div', { className: 'fs-library__right-subnav', role: 'tablist' },
        React.createElement('button', {
          type: 'button', role: 'tab', 'aria-selected': view === 'preview',
          className: 'fs-library__right-tab' + (view === 'preview' ? ' fs-library__right-tab--active' : ''),
          onClick: function () { setView('preview'); },
        }, 'Preview'),
        canActivate ? React.createElement('button', {
          type: 'button', role: 'tab', 'aria-selected': view === 'editor',
          className: 'fs-library__right-tab' + (view === 'editor' ? ' fs-library__right-tab--active' : ''),
          onClick: function () { setView('editor'); },
        }, 'Edit') : null,
        React.createElement('button', {
          type: 'button', role: 'tab', 'aria-selected': view === 'history',
          className: 'fs-library__right-tab' + (view === 'history' ? ' fs-library__right-tab--active' : ''),
          onClick: function () { setView('history'); },
        }, 'History'),
      ),

      /* Body — routed by view */
      view === 'editor'
        ? React.createElement(SchemaEditor, {
            templateId: sel.id,
            schema:     schema,
            onSaved:    function (updated) {
              ctx.setSel(updated); ctx.reload(); setView('preview');
              if (window.FS && window.FS.toast) window.FS.toast.show({ message: 'Schema saved as a new version', tone: 'success' });
            },
            onCancel: function () { setView('preview'); },
          })
        : view === 'history'
        ? React.createElement(VersionHistoryPanel, {
            templateId:   sel.id,
            latestSchema: schema,
            canManage:    canActivate,
            onRestored:   function (updated) { ctx.setSel(updated); ctx.reload(); setView('preview'); },
          })
        : /* preview */
          React.createElement(React.Fragment, null,

            /* Side-by-side: Source vs Extracted schema */
            React.createElement('div', { className: 'fs-library__review-grid' },

              React.createElement('div', { className: 'fs-library__review-panel' },
                React.createElement('h3', { className: 'fs-library__review-panel-title' }, 'Your file'),
                React.createElement('div', { className: 'fs-library__source-card' },
                  React.createElement('div', { className: 'fs-library__source-icon' }, '📄'),
                  React.createElement('div', { className: 'fs-library__source-info' },
                    React.createElement('span', { className: 'fs-library__source-filename' }, sel.title + '.docx'),
                    React.createElement('span', { className: 'fs-library__source-meta' }, RT_LABEL[sel.report_type] + ' · uploaded ' + fmtDate(sel.created_at)),
                  ),
                ),
                React.createElement('p', { className: 'fs-library__review-note' },
                  'AI read your file and identified ' + schema.sections.length + ' section' + (schema.sections.length === 1 ? '' : 's') + ' below.',
                ),
              ),

              React.createElement('div', { className: 'fs-library__review-panel' },
                React.createElement('h3', { className: 'fs-library__review-panel-title' }, 'Extracted schema'),
                React.createElement('ol', { className: 'fs-library__schema-list' },
                  schema.sections.map(function (s, i) {
                    return React.createElement('li', { key: i, className: 'fs-library__schema-item' },
                      React.createElement('span', { className: 'fs-library__schema-kind-icon', title: KIND_LABEL[s.kind] }, KIND_ICON[s.kind] || '•'),
                      React.createElement('div', { className: 'fs-library__schema-item-body' },
                        React.createElement('span', { className: 'fs-library__schema-item-title' }, s.title),
                        React.createElement('span', { className: 'fs-library__schema-item-hint' }, s.prompt_hint),
                      ),
                    );
                  }),
                ),
              ),
            ),

            /* Test render panel */
            React.createElement(TestRenderPanel, { schema: schema, reportType: sel.report_type }),

            /* CTA footer */
            canActivate && !sel.active
              ? React.createElement('div', { className: 'fs-library__cta-footer' },
                  React.createElement(Button, {
                    variant: 'primary',
                    onClick: function () { handleActivate(sel); },
                  }, '✓ Use this template'),
                  React.createElement('p', { className: 'fs-library__cta-note' },
                    'Sets this as the active default for ' + (RT_LABEL[sel.report_type] || sel.report_type).toLowerCase() + ' reports in the ' + sel.scope + ' library.',
                  ),
                )
              : null,

            sel.active
              ? React.createElement('div', { className: 'fs-library__cta-footer fs-library__cta-footer--active' },
                  React.createElement('div', { className: 'fs-library__active-confirm' },
                    React.createElement('span', { className: 'fs-library__active-confirm-icon' }, '✓'),
                    React.createElement('span', null, 'Active default for ' + (RT_LABEL[sel.report_type] || sel.report_type).toLowerCase() + ' reports'),
                  ),
                )
              : null,
          ),

    );
  }

  /* ── Test Render Panel ─────────────────────────────────────────────── */

  /* Sprint 10 follow-up — TestRender body is now max-height scrollable
     so long previews don't run off the page; "↗ Open in modal" expands
     to a full-screen modal using ModalOverlay. */
  function TestRenderPanel(props) {
    var schema     = props.schema;
    var expandRef  = React.useState(false);
    var expanded   = expandRef[0]; var setExpanded = expandRef[1];

    var modalRef   = React.useState(false);
    var modalOpen  = modalRef[0]; var setModalOpen = modalRef[1];

    var Modal      = window.FieldSight.ModalOverlay;

    if (!schema || !schema.sections) return null;

    /* Walk all sections, including nested children, into a single
       flat list so the render body shows everything. */
    function flatten(arr, depth) {
      var out = [];
      (arr || []).forEach(function (s) {
        out.push({ sec: s, depth: depth || 0 });
        if (s.children && s.children.length) {
          out = out.concat(flatten(s.children, (depth || 0) + 1));
        }
      });
      return out;
    }
    var flat = flatten(schema.sections, 0);

    function renderBody(scrollable) {
      return React.createElement('div', {
        className: 'fs-library__test-render-body'
                   + (scrollable ? ' fs-library__test-render-body--scroll' : ''),
      },
        flat.map(function (entry, i) {
          return React.createElement('div', {
            key:       i,
            className: 'fs-library__render-section'
                       + (entry.depth > 0 ? ' fs-library__render-section--child' : ''),
          },
            React.createElement('h4', { className: 'fs-library__render-section-title' },
              React.createElement('span', { className: 'fs-library__render-kind-badge' }, KIND_LABEL[entry.sec.kind] || entry.sec.kind),
              entry.sec.title,
            ),
            renderSectionSample(entry.sec),
          );
        }),
      );
    }

    return React.createElement('div', { className: 'fs-library__test-render' },
      React.createElement('div', { className: 'fs-library__test-render-header' },
        React.createElement('span', { className: 'fs-library__test-render-title' }, 'Test render'),
        React.createElement('span', { className: 'fs-library__test-render-sub' }, 'Preview with sample site data'),
        React.createElement('div', { className: 'fs-library__test-render-actions' },
          expanded && Modal ? React.createElement('button', {
            type:      'button',
            className: 'fs-library__test-render-modal-btn',
            onClick:   function () { setModalOpen(true); },
            title:     'Open in full-screen modal',
            'aria-label': 'Open preview in full-screen modal',
          }, '↗ Full preview') : null,
          React.createElement('button', {
            type:      'button',
            className: 'fs-library__test-render-toggle',
            onClick:   function () { setExpanded(function (e) { return !e; }); },
            'aria-expanded': expanded,
          }, expanded ? 'Collapse ▲' : 'Expand ▼'),
        ),
      ),

      expanded && renderBody(true),

      modalOpen && Modal ? React.createElement(Modal, {
        title:   'Test render · ' + flat.length + ' section' + (flat.length === 1 ? '' : 's'),
        onClose: function () { setModalOpen(false); },
        size:    'large',
      },
        React.createElement('div', { className: 'fs-library__test-render-modal' },
          renderBody(false),
        ),
      ) : null,
    );
  }

  function renderSectionSample(sec) {
    switch (sec.kind) {
      case 'narrative':
        return React.createElement('p', { className: 'fs-library__render-narrative' }, SAMPLE.narrative);

      case 'list':
        return React.createElement('ul', { className: 'fs-library__render-list' },
          SAMPLE.list.map(function (item, i) {
            return React.createElement('li', { key: i }, item);
          }),
        );

      case 'kpi':
        var fields = sec.fields && sec.fields.length ? sec.fields : Object.keys(SAMPLE.kpi).slice(0, 3);
        return React.createElement('div', { className: 'fs-library__render-kpi-row' },
          fields.map(function (f) {
            return React.createElement('div', { key: f, className: 'fs-library__render-kpi-tile' },
              React.createElement('span', { className: 'fs-library__render-kpi-value' }, SAMPLE.kpi[f] || '—'),
              React.createElement('span', { className: 'fs-library__render-kpi-label' }, f.replace(/_/g, ' ')),
            );
          }),
        );

      case 'table':
        var cols = sec.fields && sec.fields.length ? sec.fields : ['action', 'owner', 'due_date'];
        return React.createElement('table', { className: 'fs-library__render-table' },
          React.createElement('thead', null,
            React.createElement('tr', null,
              cols.map(function (c) {
                return React.createElement('th', { key: c }, c.replace(/_/g, ' '));
              }),
            ),
          ),
          React.createElement('tbody', null,
            SAMPLE.table.map(function (row, i) {
              return React.createElement('tr', { key: i },
                cols.map(function (c) {
                  return React.createElement('td', { key: c }, row[c] || '—');
                }),
              );
            }),
          ),
        );

      case 'photos':
        return React.createElement('div', { className: 'fs-library__render-photos' },
          SAMPLE.photos.map(function (label, i) {
            return React.createElement('div', { key: i, className: 'fs-library__render-photo-thumb' },
              React.createElement('span', { className: 'fs-library__render-photo-icon' }, '🖼'),
              React.createElement('span', { className: 'fs-library__render-photo-label' }, label),
            );
          }),
        );

      default:
        return null;
    }
  }

  /* ── Page wrappers ─────────────────────────────────────────────────── */

  function LibraryMiddleWithProvider() {
    return React.createElement(LibraryProvider, null,
      React.createElement(LibraryMiddle, null),
    );
  }

  function LibraryRightWithProvider() {
    return React.createElement(LibraryProvider, null,
      React.createElement(LibraryRight, null),
    );
  }

  /* The AppShell instantiates Middle and Right in separate subtrees, so we
     need a shared context. The pattern used by insights.js is a single
     Provider wrapping both. AppShell's 3-panel renderer must therefore
     use the Provider variant. We expose a combined wrapper. */

  function LibraryPage() {
    return React.createElement(LibraryProvider, null,
      React.createElement('div', { style: { display: 'contents' } },
        React.createElement(LibraryMiddle, null),
        React.createElement(LibraryRight, null),
      ),
    );
  }

  /* AppShell expects { Middle, Right } components that share context via
     a Provider prop. Follow the pattern of team.js / insights.js:
     expose Provider, Middle, Right separately so AppShell can wrap them. */

  if (!window.FieldSight)       window.FieldSight = {};
  if (!window.FieldSight.PAGES) window.FieldSight.PAGES = {};

  window.FieldSight.SchemaEditor        = SchemaEditor;
  window.FieldSight.VersionHistoryPanel = VersionHistoryPanel;

  window.FieldSight.PAGES['/library'] = {
    Provider: LibraryProvider,
    Middle:   LibraryMiddle,
    Right:    LibraryRight,
  };

})();
