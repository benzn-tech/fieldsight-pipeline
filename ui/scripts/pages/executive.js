/* ==========================================================================
   FieldSight /executive — Sprint 9 Track C.5
   --------------------------------------------------------------------------
   Director / C-suite dashboard. "How is the business performing?"
   - Default range: last 90 days
   - Org-level KPI block on top (project value, headcount, safety
     incidents, completion)
   - Org safety trend sparkline
   - Region rollup table
   - Click a region → right detail drill-down

   Permission gate: executive:view (director only).

   Registers as window.FieldSight.PAGES['/executive'].
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  var Ctx = React.createContext(null);

  function Provider(props) {
    var caller = (window.AuthMock && window.AuthMock.currentUser) || {};
    var depKey = (caller.name || '') + '|' + (caller.role || '') + '|' + (caller.isAdmin ? 'admin' : '');
    var canSee = window.FS && window.FS.can && window.FS.can(caller, 'executive:view');

    if (!canSee) {
      var blocked = { state: { status: 'access_denied', message: 'Executive dashboard requires the executive:view permission.' } };
      return React.createElement(Ctx.Provider, { value: blocked }, props.children);
    }

    var hook = window.FS.strategic.useStrategicState(
      { rollupKey: 'org', range: 'last90d' },
      depKey
    );
    return React.createElement(Ctx.Provider, { value: hook }, props.children);
  }

  function fmtCurrencyFull(v) {
    if (!v) return '—';
    if (v >= 1_000_000) return '$' + (v / 1_000_000).toFixed(1) + 'M';
    if (v >= 1_000)     return '$' + (v / 1_000).toFixed(0) + 'k';
    return '$' + v.toLocaleString('en-NZ');
  }

  function MiddleColumn(props) {
    var fs           = window.FieldSight;
    var KpiStrip     = fs.KpiStrip;
    var StatCard     = fs.StatCard;
    var SparkLine    = fs.SparkLine;
    var ColumnChart  = fs.ColumnChart;     /* Sprint 9.5.5 */
    var RollupTable  = fs.RollupTable;
    var HealthScore  = fs.HealthScore;
    var AccessDenied = fs.AccessDenied;
    var ErrorBanner  = fs.ErrorBanner;

    var ctx = React.useContext(Ctx);
    if (!ctx) return null;
    var state    = ctx.state;
    var range    = ctx.range;
    var setRange = ctx.setRange;
    var onSelect = props.onSelect || function () {};
    var selectedId = props.selectedItem && props.selectedItem.kind === 'region'
      ? props.selectedItem.region_id : null;

    var header = React.createElement('div', { className: 'fs-strategic__header' },
      React.createElement('h2', { className: 'fs-strategic__title' }, 'Executive'),
      React.createElement('div', { className: 'fs-strategic__subtitle' },
        'Org-wide health, safety, completion and project value across all regions.'),
    );

    if (state.status === 'access_denied') {
      return React.createElement('div', { className: 'fs-strategic' },
        header,
        AccessDenied
          ? React.createElement(AccessDenied, { scope: 'the executive dashboard', message: state.message })
          : React.createElement('div', null, state.message));
    }
    var toolbar = React.createElement(window.FS.strategic.RangeToolbar, { range: range, setRange: setRange });
    if (state.status === 'loading') {
      return React.createElement('div', { className: 'fs-strategic' },
        header, toolbar,
        React.createElement('div', { className: 'fs-strategic__loading' }, 'Aggregating org-wide…'));
    }
    if (state.status === 'error') {
      return React.createElement('div', { className: 'fs-strategic' },
        header, toolbar,
        ErrorBanner ? React.createElement(ErrorBanner, {
          message: (state.error && state.error.message) || 'Could not load executive data',
          retryable: true, onRetry: state.retry,
        }) : null);
    }

    var org     = state.data && state.data.org ? state.data.org : null;
    if (!org) {
      return React.createElement('div', { className: 'fs-strategic' },
        header, toolbar,
        React.createElement('div', { className: 'fs-strategic__empty' },
          'No organisation data yet for this range.'));
    }
    var totals  = org.totals;
    var regions = org.regions || [];
    var completionPct = totals.action_total > 0
      ? Math.round(totals.completion_rate_weighted * 100) : null;

    return React.createElement('div', { className: 'fs-strategic' },
      header,
      toolbar,

      /* Org banner: name + worst-region health + value */
      React.createElement('div', { className: 'fs-executive__banner' },
        HealthScore ? React.createElement(HealthScore, {
          grade: org.health, size: 'lg', label: org.name,
        }) : null,
        React.createElement('div', { className: 'fs-executive__banner-meta' },
          React.createElement('div', { className: 'fs-executive__banner-value' },
            fmtCurrencyFull(totals.project_value_nzd)),
          React.createElement('div', { className: 'fs-executive__banner-caption' },
            'Project value across ' + totals.site_count + ' active sites'),
        ),
      ),

      React.createElement(KpiStrip, null,
        React.createElement(StatCard, { value: totals.site_count,    label: 'Sites',  tone: 'neutral' }),
        React.createElement(StatCard, { value: totals.team_size,     label: 'Team',   tone: 'neutral' }),
        React.createElement(StatCard, {
          value: totals.safety_count, label: 'Safety incidents',
          tone:  totals.safety_count > 10 ? 'danger' : 'warning',
        }),
        React.createElement(StatCard, {
          value: completionPct != null ? completionPct + '%' : '—',
          label: 'Completion',
          tone:  completionPct != null && completionPct >= 75 ? 'success' : 'warning',
        }),
      ),

      /* Sprint 9.5.5 — 2-col row: org trend on the left, region
         health distribution on the right. */
      React.createElement('div', { className: 'fs-strategic__row-2col' },
        SparkLine ? React.createElement('div', { className: 'fs-strategic__trend' },
          React.createElement('div', { className: 'fs-strategic__trend-label' },
            'Org safety — last ' + (range === 'last90d' ? '90' : '30') + ' days'),
          React.createElement(SparkLine, {
            points: org.trend || [], tone: 'danger', width: 360, height: 56, showLastValue: true,
          }),
        ) : null,
        ColumnChart ? React.createElement('div', { className: 'fs-strategic__chart-card' },
          React.createElement('div', { className: 'fs-strategic__chart-card-label' },
            'Region health distribution'),
          React.createElement(ColumnChart, {
            data: ['A','B','C','D'].map(function (g) {
              var n = regions.filter(function (r) { return r.health === g; }).length;
              var tone = g === 'A' ? 'success' : g === 'B' ? 'info'
                       : g === 'C' ? 'warning' : 'danger';
              return { key: g, label: g, value: n, tone: tone };
            }),
            height: 120,
          }),
        ) : null,
      ),

      React.createElement('div', { className: 'fs-strategic__section' },
        React.createElement('div', { className: 'fs-strategic__section-header' },
          React.createElement('h3', { className: 'fs-strategic__section-title' },
            'Regions'),
          React.createElement('div', { className: 'fs-strategic__section-meta' },
            regions.length + ' region' + (regions.length === 1 ? '' : 's')),
        ),
        RollupTable ? React.createElement(RollupTable, {
          columns:    window.FS.strategic.regionColumns(),
          rows:       regions.map(function (r) { return Object.assign({ id: r.region_id }, r); }),
          onSelect:   function (row) {
            onSelect({ kind: 'region', id: 'region_' + row.region_id, region_id: row.region_id, region: row });
          },
          selectedId: selectedId,
        }) : null,
      ),

      React.createElement('div', { className: 'fs-executive__notes' },
        React.createElement('div', { className: 'fs-executive__notes-title' }, 'Reading this dashboard'),
        React.createElement('ul', { className: 'fs-executive__notes-list' },
          React.createElement('li', null, 'Org health = worst-of-regions; regions = worst-of-projects.'),
          React.createElement('li', null, 'Completion is weighted by total open + closed actions across the org.'),
          React.createElement('li', null, 'Drill in via /portfolio (per project) or /regional (per region).'),
        ),
      ),
    );
  }

  function RightDetail(props) {
    var fs        = window.FieldSight;
    var SparkLine = fs.SparkLine;
    var HealthScore = fs.HealthScore;
    var IconBtn   = fs.IconButton;

    var sel = props.selectedItem;
    if (!sel || sel.kind !== 'region') {
      return React.createElement('div', { className: 'fs-strategic-detail__placeholder' },
        React.createElement('div', { className: 'fs-strategic-detail__placeholder-title' },
          'Select a region'),
        React.createElement('div', { className: 'fs-strategic-detail__placeholder-body' },
          'Pick a region row for a per-region drill-down.'),
      );
    }
    var r = sel.region;
    return React.createElement('div', { className: 'fs-strategic-detail' },
      React.createElement('div', { className: 'fs-strategic-detail__header' },
        React.createElement('div', { className: 'fs-strategic-detail__header-text' },
          React.createElement('h2', { className: 'fs-strategic-detail__name' }, r.name),
          React.createElement('div', { className: 'fs-strategic-detail__sub' },
            (r.country || '—') + ' · ' + r.site_count + ' site' + (r.site_count === 1 ? '' : 's')),
        ),
        IconBtn ? React.createElement(IconBtn, {
          icon: 'x', ariaLabel: 'Close detail', size: 'sm',
          onClick: function () { if (props.onClose) props.onClose(); },
        }) : null,
      ),
      React.createElement('div', { className: 'fs-strategic-detail__health-row' },
        HealthScore ? React.createElement(HealthScore, { grade: r.health, size: 'lg' }) : null,
        SparkLine ? React.createElement(SparkLine, {
          points: r.trend || [], tone: 'danger', width: 280, height: 56, showLastValue: true,
        }) : null,
      ),
      React.createElement('div', { className: 'fs-strategic-detail__rows' },
        React.createElement(DetailRow, { label: 'Sites',          value: r.site_count }),
        React.createElement(DetailRow, { label: 'Team size',      value: r.team_size + ' people' }),
        React.createElement(DetailRow, { label: 'Safety',         value: r.safety_count + ' incidents' }),
        React.createElement(DetailRow, { label: 'Quality',        value: r.quality_count + ' issues' }),
        React.createElement(DetailRow, { label: 'Completion',
          value: r.action_total > 0
            ? Math.round(r.completion_rate_weighted * 100) + '% (' + r.action_done + '/' + r.action_total + ')'
            : 'No actions tracked' }),
      ),
    );
  }

  function DetailRow(props) {
    return React.createElement('div', { className: 'fs-strategic-detail__row' },
      React.createElement('div', { className: 'fs-strategic-detail__row-label' }, props.label),
      React.createElement('div', { className: 'fs-strategic-detail__row-value' }, props.value),
    );
  }

  if (!window.FieldSight) window.FieldSight = {};
  if (!window.FieldSight.PAGES) window.FieldSight.PAGES = {};
  window.FieldSight.PAGES['/executive'] = {
    Provider: Provider,
    Middle:   MiddleColumn,
    Right:    RightDetail,
    layout:   'full-width',   /* Sprint 9.5.1 — 2-panel canvas, drill-down via RightDrawer */
  };

})();
