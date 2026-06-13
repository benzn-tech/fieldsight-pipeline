/* ==========================================================================
   FieldSight Strategic Pages · Shared composition (Sprint 9 Track C)
   --------------------------------------------------------------------------
   /portfolio · /regional · /executive share the same data shape (per-
   site rollup → optional region grouping → optional org rollup) and
   the same rendering primitives (KpiStrip + RollupTable + SparkLine
   trend). This file owns the shared bits so each page only declares
   its scope (range + perm + columns) and the differential title /
   KPI emphasis.

   Public API:
     window.FS.strategic = {
       useStrategicState({ rollupKey: 'projects' | 'regions' | 'org',
                           range: 'last30d' | 'last90d' }, deps)
       defaultRange(mode)
       projectColumns(opts), regionColumns(opts)
       PageHeader, KpiCard helpers
     }

   Pages call useStrategicState() inside their Provider and pass the
   resulting state to the rendering scaffolds.
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  function defaultRange(mode) {
    var today = window.FS.api.todayNZDT();
    var span = mode === 'last90d' ? 89 : 29;
    return {
      from:  window.FS.api.addDaysISO(today, -span),
      to:    today,
      mode:  mode,
      today: today,
    };
  }

  /* ─── Provider hook — used by all 3 pages ────────────────────────── */

  function useStrategicState(opts, depKey) {
    var rollupKey = opts.rollupKey;       /* 'projects' | 'regions' | 'org' */
    var rangeMode = opts.range || 'last30d';

    var refRange = React.useState(rangeMode);
    var range    = refRange[0];
    var setRange = refRange[1];

    var refState = React.useState({ status: 'loading' });
    var state    = refState[0];
    var setState = refState[1];

    var refRetry = React.useState(0);
    var retry    = refRetry[0];
    var setRetry = refRetry[1];

    React.useEffect(function () {
      var cancelled = false;
      setState({ status: 'loading' });

      var rangeObj = defaultRange(range);
      var strategic = window.FS && window.FS.api && window.FS.api.strategic;
      if (!strategic) {
        setState({ status: 'error', error: { code: 0, message: 'Strategic aggregator not loaded', retryable: false } });
        return undefined;
      }
      var promise;
      if (rollupKey === 'projects')      promise = strategic.getProjectRollup(rangeObj);
      else if (rollupKey === 'regions')  promise = strategic.getRegionRollup(rangeObj);
      else                                promise = strategic.getOrgRollup(rangeObj);

      promise.then(function (res) {
        if (cancelled) return;
        if (res && res._accessDenied) {
          setState({ status: 'access_denied', message: res.error });
          return;
        }
        setState({
          status: 'ok',
          range:  rangeObj,
          data:   res,
        });
      }).catch(function (err) {
        if (cancelled) return;
        setState({
          status: 'error',
          error: {
            code:      (err && err.status)  || 0,
            message:   (err && err.message) || 'Could not load',
            retryable: true,
          },
          retry:  function () { setRetry(function (n) { return n + 1; }); },
        });
      });

      return function () { cancelled = true; };
    }, [range, retry, depKey]);

    return {
      state:    state,
      range:    range,
      setRange: setRange,
    };
  }

  /* ─── Column definitions (shared between portfolio/regional) ─────── */

  function projectColumns(opts) {
    opts = opts || {};
    return [
      { key: 'name',          label: 'Project',  type: 'text',
        sortKey: function (r) { return (r.name || '').toLowerCase(); }, align: 'left' },
      { key: 'health',        label: 'Health',   type: 'health',  align: 'center' },
      { key: 'safety_count',  label: 'Safety',   type: 'num',     align: 'right' },
      { key: 'quality_count', label: 'Quality',  type: 'num',     align: 'right' },
      { key: 'completion_rate', label: 'Done',   type: 'percent', align: 'right' },
      { key: 'team_size',     label: 'Team',     type: 'num',     align: 'right' },
      opts.showRegion ? { key: 'region_name', label: 'Region',   type: 'text',  align: 'left' } : null,
      { key: 'trend',         label: '14-day',   type: 'trend',   tone: 'danger' },
    ].filter(Boolean);
  }

  function regionColumns() {
    return [
      { key: 'name',         label: 'Region',  type: 'text',   align: 'left',
        sortKey: function (r) { return (r.name || '').toLowerCase(); } },
      { key: 'health',       label: 'Health',  type: 'health', align: 'center' },
      { key: 'site_count',   label: 'Sites',   type: 'num',    align: 'right' },
      { key: 'team_size',    label: 'Team',    type: 'num',    align: 'right' },
      { key: 'safety_count', label: 'Safety',  type: 'num',    align: 'right' },
      { key: 'quality_count',label: 'Quality', type: 'num',    align: 'right' },
      { key: 'completion_rate_weighted', label: 'Done', type: 'percent', align: 'right',
        sortKey: function (r) { return r.completion_rate_weighted; } },
      { key: 'trend',        label: '14-day',  type: 'trend',  tone: 'danger' },
    ];
  }

  /* ─── Range toolbar ──────────────────────────────────────────────── */

  function RangeToolbar(props) {
    var range    = props.range;
    var setRange = props.setRange;
    var modes    = props.modes || [
      { value: 'last30d', label: 'Last 30 days' },
      { value: 'last90d', label: 'Last 90 days' },
    ];
    return React.createElement('div', {
      className: 'fs-strategic__toolbar',
      role:      'group',
      'aria-label': 'Date range',
    },
      modes.map(function (m) {
        var active = range === m.value;
        return React.createElement('button', {
          key:       m.value,
          type:      'button',
          className: 'fs-strategic__chip' + (active ? ' fs-strategic__chip--active' : ''),
          onClick:   function () { setRange(m.value); },
        }, m.label);
      }),
    );
  }

  if (!window.FS) window.FS = {};
  window.FS.strategic = {
    defaultRange:        defaultRange,
    useStrategicState:   useStrategicState,
    projectColumns:      projectColumns,
    regionColumns:       regionColumns,
    RangeToolbar:        RangeToolbar,
  };

})();
