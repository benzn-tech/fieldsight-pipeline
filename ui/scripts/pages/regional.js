/* ==========================================================================
   FieldSight /regional — Sprint 9 Track C.4
   --------------------------------------------------------------------------
   GM dashboard. "How is my region tracking?"
   - Default range: last 90 days (longer than portfolio for trend
     significance)
   - Region rollup table on top, project rollup grouped by region below
   - Click a region → right detail with all projects under it

   Permission gate: regional:view (GM only).

   Registers as window.FieldSight.PAGES['/regional'].
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  var Ctx = React.createContext(null);

  function Provider(props) {
    var caller = (window.AuthMock && window.AuthMock.currentUser) || {};
    var depKey = (caller.name || '') + '|' + (caller.role || '') + '|' + (caller.isAdmin ? 'admin' : '');
    var canSee = window.FS && window.FS.can && window.FS.can(caller, 'regional:view');

    if (!canSee) {
      var blocked = { state: { status: 'access_denied', message: 'Regional dashboard requires the regional:view permission.' } };
      return React.createElement(Ctx.Provider, { value: blocked }, props.children);
    }

    var hook = window.FS.strategic.useStrategicState(
      { rollupKey: 'regions', range: 'last90d' },
      depKey
    );
    return React.createElement(Ctx.Provider, { value: hook }, props.children);
  }

  function MiddleColumn(props) {
    var fs           = window.FieldSight;
    var KpiStrip     = fs.KpiStrip;
    var StatCard     = fs.StatCard;
    var SparkLine    = fs.SparkLine;
    var ColumnChart  = fs.ColumnChart;     /* Sprint 9.5.5 */
    var RollupTable  = fs.RollupTable;
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
      React.createElement('h2', { className: 'fs-strategic__title' }, 'Regional'),
      React.createElement('div', { className: 'fs-strategic__subtitle' },
        'Region-wide health, safety incidents and completion across all sites.'),
    );

    if (state.status === 'access_denied') {
      return React.createElement('div', { className: 'fs-strategic' },
        header,
        AccessDenied
          ? React.createElement(AccessDenied, { scope: 'the regional dashboard', message: state.message })
          : React.createElement('div', null, state.message));
    }
    var toolbar = React.createElement(window.FS.strategic.RangeToolbar, { range: range, setRange: setRange });
    if (state.status === 'loading') {
      return React.createElement('div', { className: 'fs-strategic' },
        header, toolbar,
        React.createElement('div', { className: 'fs-strategic__loading' }, 'Aggregating across regions…'));
    }
    if (state.status === 'error') {
      return React.createElement('div', { className: 'fs-strategic' },
        header, toolbar,
        ErrorBanner ? React.createElement(ErrorBanner, {
          message: (state.error && state.error.message) || 'Could not load regional data',
          retryable: true, onRetry: state.retry,
        }) : null);
    }

    var regions = (state.data && state.data.regions) || [];
    var totalSites    = regions.reduce(function (a, r) { return a + r.site_count; }, 0);
    var totalTeam     = regions.reduce(function (a, r) { return a + r.team_size; }, 0);
    var totalSafety   = regions.reduce(function (a, r) { return a + r.safety_count; }, 0);
    var totalActions  = regions.reduce(function (a, r) { return a + r.action_total; }, 0);
    var totalDone     = regions.reduce(function (a, r) { return a + r.action_done; }, 0);
    var completionPct = totalActions > 0 ? Math.round((totalDone / totalActions) * 100) : null;

    /* Aggregate trend across regions. */
    var totalTrend = [];
    if (regions.length > 0 && regions[0].trend && regions[0].trend.length > 0) {
      var trendDates = regions[0].trend.map(function (t) { return t.date; });
      totalTrend = trendDates.map(function (dd, idx) {
        var sum = regions.reduce(function (a, r) {
          return a + ((r.trend[idx] && r.trend[idx].value) || 0);
        }, 0);
        return { date: dd, value: sum };
      });
    }

    return React.createElement('div', { className: 'fs-strategic' },
      header,
      toolbar,
      React.createElement(KpiStrip, null,
        React.createElement(StatCard, { value: regions.length, label: 'Regions', tone: 'neutral' }),
        React.createElement(StatCard, { value: totalSites,     label: 'Sites',   tone: 'neutral' }),
        React.createElement(StatCard, {
          value: totalSafety, label: 'Safety incidents',
          tone:  totalSafety > 0 ? 'danger' : 'neutral',
        }),
        React.createElement(StatCard, {
          value: completionPct != null ? completionPct + '%' : '—',
          label: 'Completion',
          tone:  completionPct != null && completionPct >= 75 ? 'success' : 'warning',
        }),
      ),

      /* Sprint 9.5.5 — 2-col row: aggregated trend on the left,
         per-region health distribution on the right. */
      React.createElement('div', { className: 'fs-strategic__row-2col' },
        SparkLine ? React.createElement('div', { className: 'fs-strategic__trend' },
          React.createElement('div', { className: 'fs-strategic__trend-label' },
            'Safety — last ' + (range === 'last90d' ? '90' : '30') + ' days, all regions'),
          React.createElement(SparkLine, {
            points: totalTrend, tone: 'danger', width: 360, height: 56, showLastValue: true,
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
            'Regions ranked by health'),
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
          'Pick a row to see all projects in that region.'),
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
        HealthScore ? React.createElement(HealthScore, {
          grade: r.health, size: 'lg', label: 'Worst-of-projects',
        }) : null,
        SparkLine ? React.createElement(SparkLine, {
          points: r.trend || [], tone: 'danger',
          width: 280, height: 56, showLastValue: true,
        }) : null,
      ),
      React.createElement('div', { className: 'fs-strategic-detail__rows' },
        React.createElement(DetailRow, { label: 'Sites',         value: r.site_count }),
        React.createElement(DetailRow, { label: 'Team size',     value: r.team_size + ' people' }),
        React.createElement(DetailRow, { label: 'Safety issues', value: r.safety_count }),
        React.createElement(DetailRow, { label: 'Quality issues',value: r.quality_count }),
        React.createElement(DetailRow, { label: 'Completion',
          value: r.action_total > 0
            ? Math.round(r.completion_rate_weighted * 100) + '% (' + r.action_done + '/' + r.action_total + ')'
            : 'No actions tracked' }),
        React.createElement(DetailRow, { label: 'Overdue actions', value: r.action_overdue }),
      ),
      React.createElement('div', { className: 'fs-strategic-detail__rows fs-strategic-detail__projects' },
        React.createElement('div', { className: 'fs-strategic-detail__rows-header' },
          'Projects in this region'),
        (r.projects || []).map(function (p) {
          return React.createElement('div', {
            key:       p.site_id,
            className: 'fs-strategic-detail__project',
          },
            HealthScore ? React.createElement(HealthScore, { grade: p.health, size: 'sm' }) : null,
            React.createElement('div', { className: 'fs-strategic-detail__project-name' }, p.name),
            React.createElement('div', { className: 'fs-strategic-detail__project-meta' },
              p.safety_count + ' safety · ' + p.quality_count + ' quality'),
          );
        }),
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
  window.FieldSight.PAGES['/regional'] = {
    Provider: Provider,
    Middle:   MiddleColumn,
    Right:    RightDetail,
    layout:   'full-width',   /* Sprint 9.5.1 — 2-panel canvas, drill-down via RightDrawer */
  };

})();
