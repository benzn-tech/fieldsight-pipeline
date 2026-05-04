/* ==========================================================================
   FieldSight SearchPalette — Sprint 8.6
   --------------------------------------------------------------------------
   Full-screen search overlay opened via Cmd/Ctrl+K or the search icon
   button in the middle-column header.

   Search scope (client-side, against cached API data):
     Tasks    — action_items[].text across last 14 days
     Safety   — safety_flags + safety_observations
     Sites    — sites[].name + sites[].location
     People   — users derived from sites[].users

   Data is loaded once on first open and cached in module scope for the
   rest of the session (survives palette close/reopen).

   Keyboard:
     ArrowUp / ArrowDown  — navigate result list
     Enter                — open selected result's canonical page
     Escape               — close

   Recent searches stored in sessionStorage (max 5).

   Exported to: window.FieldSight.SearchPalette
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  var TYPE_ORDER  = ['task', 'safety', 'site', 'user'];
  var TYPE_LABELS = { task: 'Tasks', safety: 'Safety', site: 'Sites', user: 'People' };
  var TYPE_ICONS  = { task: 'check-square', safety: 'shield-alert', site: 'building-2', user: 'user' };

  /* ---------------------------------------------------------------------- */
  /* Module-level data cache — survives palette close/reopen in a session    */
  /* ---------------------------------------------------------------------- */

  var _cache = { loaded: false, loading: false,
                 tasks: [], safety: [], sites: [], users: [] };

  async function _loadCache() {
    if (_cache.loaded || _cache.loading) return;
    _cache.loading = true;
    try {
      var today = window.FS.api.todayNZDT();
      var from  = window.FS.api.addDaysISO(today, -14);

      var results = await Promise.all([
        window.FS.api.sites.getSites()
          .catch(function () { return { sites: [] }; }),
        window.FS.api.tasks.getActionsResolvedRange({ from: from, to: today })
          .catch(function () { return { rows: [] }; }),
        window.FS.api.compliance.getSafetyRange({ from: from, to: today })
          .catch(function () { return { rows: [] }; }),
      ]);

      _cache.sites  = (results[0] && results[0].sites) || [];
      _cache.tasks  = (results[1] && results[1].rows)  || [];
      _cache.safety = (results[2] && results[2].rows)  || [];

      /* Derive unique users from sites */
      var usersMap = {};
      _cache.sites.forEach(function (site) {
        (site.users || []).forEach(function (u) {
          if (u.name && !usersMap[u.name]) usersMap[u.name] = u;
        });
      });
      _cache.users  = Object.keys(usersMap).map(function (k) { return usersMap[k]; });
      _cache.loaded = true;
    } catch (e) {
      console.warn('[SearchPalette] cache preload failed', e);
    }
    _cache.loading = false;
  }

  /* ---------------------------------------------------------------------- */
  /* Client-side search — synchronous, runs against cache                    */
  /* ---------------------------------------------------------------------- */

  function _search(q) {
    var ql = q.toLowerCase();
    var byType = { task: [], safety: [], site: [], user: [] };

    _cache.tasks.forEach(function (row) {
      if (byType.task.length >= 5) return;
      if (row.action && row.action.toLowerCase().indexOf(ql) !== -1) {
        byType.task.push({
          type:     'task',
          id:       row.id,
          title:    row.action,
          subtitle: row.responsible ? 'Owner: ' + row.responsible : row.date,
          route:    '/tasks',
        });
      }
    });

    _cache.safety.forEach(function (row) {
      if (byType.safety.length >= 5) return;
      var obs = row.observation || '';
      if (obs.toLowerCase().indexOf(ql) !== -1) {
        byType.safety.push({
          type:     'safety',
          id:       row.id,
          title:    obs.length > 80 ? obs.slice(0, 80) + '…' : obs,
          subtitle: 'Risk: ' + (row.risk_level || 'unknown') + ' \xb7 ' + row.date,
          route:    '/safety',
        });
      }
    });

    _cache.sites.forEach(function (site) {
      if (byType.site.length >= 5) return;
      var match = (site.name && site.name.toLowerCase().indexOf(ql) !== -1)
               || (site.location && site.location.toLowerCase().indexOf(ql) !== -1);
      if (match) {
        byType.site.push({
          type:     'site',
          id:       site.site_id || site.name,
          title:    site.name,
          subtitle: site.location || '',
          route:    '/sites',
        });
      }
    });

    _cache.users.forEach(function (u) {
      if (byType.user.length >= 5) return;
      var match = (u.name && u.name.toLowerCase().indexOf(ql) !== -1)
               || (u.role && u.role.toLowerCase().indexOf(ql) !== -1);
      if (match) {
        byType.user.push({
          type:     'user',
          id:       u.name,
          title:    u.name,
          subtitle: u.role || '',
          route:    '/team',
        });
      }
    });

    var out = [];
    TYPE_ORDER.forEach(function (t) { out = out.concat(byType[t]); });
    return out;
  }

  /* ---------------------------------------------------------------------- */
  /* SearchPalette component                                                  */
  /* ---------------------------------------------------------------------- */

  function SearchPalette(props) {
    var NavIcon = window.FieldSight && window.FieldSight.NavIcon;
    var onClose = props.onClose || function () {};

    var refQ    = React.useState('');
    var query   = refQ[0]; var setQuery = refQ[1];

    var refRes  = React.useState([]);
    var results = refRes[0]; var setResults = refRes[1];

    var refIdx  = React.useState(0);
    var selIdx  = refIdx[0]; var setSelIdx = refIdx[1];

    var refRecent = React.useState(function () {
      try { return JSON.parse(sessionStorage.getItem('fs.search.recent') || '[]'); }
      catch (_) { return []; }
    });
    var recent  = refRecent[0]; var setRecent = refRecent[1];

    var inputRef = React.useRef(null);
    var listRef  = React.useRef(null);

    /* Auto-focus + kick off data preload on open */
    React.useEffect(function () {
      if (inputRef.current) inputRef.current.focus();
      _loadCache();
    }, []);

    /* Re-run search whenever query changes */
    React.useEffect(function () {
      var q = query.trim();
      if (!q) { setResults([]); setSelIdx(0); return; }
      setResults(_search(q));
      setSelIdx(0);
    }, [query]);

    /* Scroll the focused result into view */
    React.useEffect(function () {
      if (!listRef.current) return;
      var el = listRef.current.querySelector('[data-selected="true"]');
      if (el) el.scrollIntoView({ block: 'nearest' });
    }, [selIdx]);

    function doSelect(item) {
      if (!item) return;
      var q = query.trim();
      if (q) {
        var next = [q].concat(recent.filter(function (r) { return r !== q; })).slice(0, 5);
        setRecent(next);
        try { sessionStorage.setItem('fs.search.recent', JSON.stringify(next)); } catch (_) {}
      }
      window.FS.Router.navigate(item.route);
      onClose();
    }

    function onKeyDown(e) {
      switch (e.key) {
        case 'Escape':
          onClose();
          break;
        case 'ArrowDown':
          e.preventDefault();
          setSelIdx(function (i) { return Math.min(i + 1, results.length - 1); });
          break;
        case 'ArrowUp':
          e.preventDefault();
          setSelIdx(function (i) { return Math.max(i - 1, 0); });
          break;
        case 'Enter':
          doSelect(results[selIdx]);
          break;
        default:
          break;
      }
    }

    /* Group results by type for sectioned display */
    var groups = [];
    TYPE_ORDER.forEach(function (type) {
      var items = results.filter(function (r) { return r.type === type; });
      if (items.length) groups.push({ type: type, items: items });
    });
    var flatItems = groups.reduce(function (acc, g) { return acc.concat(g.items); }, []);

    return React.createElement('div', {
      className:    'fs-search-palette',
      role:         'dialog',
      'aria-modal': 'true',
      'aria-label': 'Search',
    },
      /* Backdrop — clicking closes */
      React.createElement('div', {
        className:   'fs-search-palette__backdrop',
        onClick:     onClose,
        'aria-hidden': 'true',
      }),

      /* Panel */
      React.createElement('div', { className: 'fs-search-palette__panel' },

        /* ---- Input row -------------------------------------------------- */
        React.createElement('div', { className: 'fs-search-palette__input-row' },
          NavIcon && React.createElement(NavIcon, {
            name:  'search',
            size:  18,
            color: 'var(--text-tertiary)',
          }),
          React.createElement('input', {
            ref:          inputRef,
            type:         'text',
            className:    'fs-search-palette__input',
            placeholder:  'Search tasks, sites, people, safety…',
            value:        query,
            onChange:     function (e) { setQuery(e.target.value); },
            onKeyDown:    onKeyDown,
            'aria-label': 'Search',
            autoComplete: 'off',
            spellCheck:   false,
          }),
          query
            ? React.createElement('button', {
                type:         'button',
                className:    'fs-search-palette__clear',
                onClick:      function () {
                  setQuery('');
                  if (inputRef.current) inputRef.current.focus();
                },
                'aria-label': 'Clear search',
              }, '\xd7')
            : null,
          React.createElement('kbd', { className: 'fs-search-palette__esc-hint' }, 'Esc'),
        ),

        /* ---- Results list ---------------------------------------------- */
        React.createElement('div', {
          ref:          listRef,
          className:    'fs-search-palette__results',
          role:         'listbox',
          'aria-label': 'Search results',
        },
          /* No match */
          query.trim() && results.length === 0
            ? React.createElement('div', { className: 'fs-search-palette__empty' },
                'No results for “' + query.trim() + '”')

            /* Grouped results */
            : query.trim() && results.length > 0
            ? groups.map(function (group) {
                return React.createElement('div', {
                  key:       group.type,
                  className: 'fs-search-palette__group',
                },
                  React.createElement('div', {
                    className: 'fs-search-palette__group-label',
                    role:      'presentation',
                  }, TYPE_LABELS[group.type] || group.type),
                  group.items.map(function (item) {
                    var idx   = flatItems.indexOf(item);
                    var isSel = idx === selIdx;
                    return React.createElement('button', {
                      key:             item.type + '_' + item.id,
                      type:            'button',
                      role:            'option',
                      'aria-selected': isSel,
                      'data-selected': String(isSel),
                      className:       'fs-search-palette__result'
                                        + (isSel ? ' fs-search-palette__result--active' : ''),
                      onClick:         function () { doSelect(item); },
                      onMouseEnter:    function () { setSelIdx(idx); },
                    },
                      NavIcon && React.createElement(NavIcon, {
                        name:  TYPE_ICONS[item.type] || 'file',
                        size:  15,
                        color: 'var(--text-tertiary)',
                      }),
                      React.createElement('div', { className: 'fs-search-palette__result-body' },
                        React.createElement('span', { className: 'fs-search-palette__result-title' },
                          item.title),
                        item.subtitle
                          ? React.createElement('span', { className: 'fs-search-palette__result-sub' },
                              item.subtitle)
                          : null,
                      ),
                    );
                  }),
                );
              })

            /* Recent searches (shown when input is empty) */
            : !query.trim() && recent.length > 0
            ? React.createElement('div', { className: 'fs-search-palette__group' },
                React.createElement('div', {
                  className: 'fs-search-palette__group-label',
                  role:      'presentation',
                }, 'Recent searches'),
                recent.map(function (r) {
                  return React.createElement('button', {
                    key:       r,
                    type:      'button',
                    className: 'fs-search-palette__result',
                    onClick:   function () { setQuery(r); },
                  },
                    NavIcon && React.createElement(NavIcon, {
                      name:  'clock',
                      size:  15,
                      color: 'var(--text-tertiary)',
                    }),
                    React.createElement('div', { className: 'fs-search-palette__result-body' },
                      React.createElement('span', {
                        className: 'fs-search-palette__result-title',
                      }, r),
                    ),
                  );
                }),
              )

            /* Empty prompt */
            : React.createElement('div', { className: 'fs-search-palette__hint' },
                'Type to search across tasks, safety flags, sites, and people'),
        ),
      ),
    );
  }

  if (!window.FieldSight) window.FieldSight = {};
  window.FieldSight.SearchPalette = SearchPalette;
})();
