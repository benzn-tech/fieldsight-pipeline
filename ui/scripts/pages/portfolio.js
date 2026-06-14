/* ==========================================================================
   FieldSight /portfolio — Sprint 9 Track C.3
   --------------------------------------------------------------------------
   Construction Manager dashboard. "All my projects on one page."
   - Default range: last 30 days
   - Per-project rollup table with health A/B/C/D
   - 4-card KPI strip + 30-day org safety trend
   - Click a project → right detail with deeper stats + sparkline

   Permission gate: portfolio:view (already on construction_manager
   per fs-globals.js + roles.js).

   Registers as window.FieldSight.PAGES['/portfolio'].
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  var Ctx = React.createContext(null);

  function Provider(props) {
    var caller = (window.AuthMock && window.AuthMock.currentUser) || {};
    var depKey = (caller.name || '') + '|' + (caller.role || '') + '|' + (caller.isAdmin ? 'admin' : '');
    var canSee = window.FS && window.FS.can && window.FS.can(caller, 'portfolio:view');

    if (!canSee) {
      var ctxBlocked = { state: { status: 'access_denied', message: 'Portfolio requires the portfolio:view permission.' } };
      return React.createElement(Ctx.Provider, { value: ctxBlocked }, props.children);
    }

    var hook = window.FS.strategic.useStrategicState(
      { rollupKey: 'projects', range: 'last30d' },
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
    var HealthScore  = fs.HealthScore;
    var TrendPill    = fs.TrendPill;
    var AccessDenied = fs.AccessDenied;
    var ErrorBanner  = fs.ErrorBanner;

    var ctx = React.useContext(Ctx);
    if (!ctx) return null;
    var state    = ctx.state;
    var range    = ctx.range;
    var setRange = ctx.setRange;
    var onSelect = props.onSelect || function () {};
    var selectedId = props.selectedItem && props.selectedItem.kind === 'project'
      ? props.selectedItem.site_id : null;

    var header = React.createElement('div', { className: 'fs-strategic__header' },
      React.createElement('h2', { className: 'fs-strategic__title' }, 'Portfolio'),
      React.createElement('div', { className: 'fs-strategic__subtitle' },
        'All projects under your management — health, safety, quality, completion.'),
    );

    if (state.status === 'access_denied') {
      return React.createElement('div', { className: 'fs-strategic' },
        header,
        AccessDenied
          ? React.createElement(AccessDenied, { scope: 'the portfolio dashboard', message: state.message })
          : React.createElement('div', null, state.message));
    }

    var toolbar = React.createElement(window.FS.strategic.RangeToolbar, {
      range: range, setRange: setRange,
    });

    if (state.status === 'loading') {
      return React.createElement('div', { className: 'fs-strategic' },
        header, toolbar,
        React.createElement('div', { className: 'fs-strategic__loading' }, 'Aggregating across projects…'));
    }
    if (state.status === 'error') {
      return React.createElement('div', { className: 'fs-strategic' },
        header, toolbar,
        ErrorBanner ? React.createElement(ErrorBanner, {
          message: (state.error && state.error.message) || 'Could not load portfolio',
          retryable: true, onRetry: state.retry,
        }) : null);
    }

    var projects = (state.data && state.data.projects) || [];
    var totals = projects.reduce(function (acc, p) {
      acc.safety  += p.safety_count;
      acc.quality += p.quality_count;
      acc.team    += p.team_size;
      acc.value   += p.project_value_nzd || 0;
      acc.action_total += p.action_total;
      acc.action_done  += p.action_done;
      acc.action_overdue += p.action_overdue;
      return acc;
    }, { safety: 0, quality: 0, team: 0, value: 0, action_total: 0, action_done: 0, action_overdue: 0 });
    var completionPct = totals.action_total > 0
      ? Math.round((totals.action_done / totals.action_total) * 100)
      : null;

    /* Org-level trend (sum across projects per day). */
    var orgTrend = projects.length > 0 && projects[0].trend
      ? projects[0].trend.map(function (t, i) {
          var sum = projects.reduce(function (acc, p) {
            return acc + ((p.trend[i] && p.trend[i].value) || 0);
          }, 0);
          return { date: t.date, value: sum };
        })
      : [];

    return React.createElement('div', { className: 'fs-strategic' },
      header,
      toolbar,
      React.createElement(KpiStrip, null,
        React.createElement(StatCard, { value: projects.length, label: 'Projects',  tone: 'neutral' }),
        React.createElement(StatCard, { value: totals.team,     label: 'Team size', tone: 'neutral' }),
        React.createElement(StatCard, {
          value: totals.safety, label: 'Safety issues',
          tone:  totals.safety > 0 ? 'danger' : 'neutral',
        }),
        React.createElement(StatCard, {
          value: completionPct != null ? completionPct + '%' : '—',
          label: 'Completion',
          tone:  completionPct != null && completionPct >= 75 ? 'success' : 'warning',
        }),
      ),

      /* Sprint 9.5.5 — full-width 2-col row: org trend on the left,
         project-health-grade distribution on the right. */
      React.createElement('div', { className: 'fs-strategic__row-2col' },
        SparkLine ? React.createElement('div', { className: 'fs-strategic__trend' },
          React.createElement('div', { className: 'fs-strategic__trend-label' },
            'Safety — last ' + (range === 'last90d' ? '90' : '30') + ' days'),
          React.createElement(SparkLine, {
            points: orgTrend, tone: 'danger', width: 360, height: 56, showLastValue: true,
          }),
        ) : null,
        ColumnChart ? React.createElement('div', { className: 'fs-strategic__chart-card' },
          React.createElement('div', { className: 'fs-strategic__chart-card-label' },
            'Project health distribution'),
          React.createElement(ColumnChart, {
            data: ['A','B','C','D'].map(function (g) {
              var n = projects.filter(function (p) { return p.health === g; }).length;
              var tone = g === 'A' ? 'success' : g === 'B' ? 'info'
                       : g === 'C' ? 'warning' : 'danger';
              return { key: g, label: g, value: n, tone: tone };
            }),
            height:   120,
          }),
        ) : null,
      ),

      /* Rollup table */
      React.createElement('div', { className: 'fs-strategic__section' },
        React.createElement('div', { className: 'fs-strategic__section-header' },
          React.createElement('h3', { className: 'fs-strategic__section-title' },
            'Projects ranked by health'),
          React.createElement('div', { className: 'fs-strategic__section-meta' },
            projects.length + ' project' + (projects.length === 1 ? '' : 's')),
        ),
        RollupTable ? React.createElement(RollupTable, {
          columns:    window.FS.strategic.projectColumns({ showRegion: true }),
          rows:       projects.map(function (p) { return Object.assign({ id: p.site_id }, p); }),
          onSelect:   function (row) {
            onSelect({ kind: 'project', id: 'project_' + row.site_id, site_id: row.site_id, project: row });
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
    if (!sel || sel.kind !== 'project') {
      return React.createElement('div', { className: 'fs-strategic-detail__placeholder' },
        React.createElement('div', { className: 'fs-strategic-detail__placeholder-title' },
          'Select a project'),
        React.createElement('div', { className: 'fs-strategic-detail__placeholder-body' },
          'Pick a row to drill into safety, quality, completion, and team metrics.'),
      );
    }
    var p = sel.project;

    return React.createElement('div', { className: 'fs-strategic-detail' },
      React.createElement('div', { className: 'fs-strategic-detail__header' },
        React.createElement('div', { className: 'fs-strategic-detail__header-text' },
          React.createElement('h2', { className: 'fs-strategic-detail__name' }, p.name),
          React.createElement('div', { className: 'fs-strategic-detail__sub' },
            (p.client || '—') + (p.location ? ' · ' + p.location : '')),
        ),
        IconBtn ? React.createElement(IconBtn, {
          icon: 'x', ariaLabel: 'Close detail', size: 'sm',
          onClick: function () { if (props.onClose) props.onClose(); },
        }) : null,
      ),
      React.createElement('div', { className: 'fs-strategic-detail__health-row' },
        HealthScore ? React.createElement(HealthScore, {
          grade: p.health, size: 'lg', label: 'Health',
        }) : null,
        SparkLine ? React.createElement(SparkLine, {
          points: p.trend || [], tone: 'danger',
          width: 280, height: 56, showLastValue: true,
        }) : null,
      ),
      React.createElement('div', { className: 'fs-strategic-detail__rows' },
        React.createElement(DetailRow, { label: 'Region',
          value: p.region_name || '—' }),
        React.createElement(DetailRow, { label: 'Project value',
          value: p.project_value_nzd ? '$' + p.project_value_nzd.toLocaleString('en-NZ') : '—' }),
        React.createElement(DetailRow, { label: 'Planned completion',
          value: p.planned_completion || '—' }),
        React.createElement(DetailRow, { label: 'Team size',
          value: p.team_size + ' people' }),
        React.createElement(DetailRow, { label: 'Safety issues',
          value: p.safety_count + ' (' + p.safety_high + ' high-risk)' }),
        React.createElement(DetailRow, { label: 'Quality issues',
          value: p.quality_count }),
        React.createElement(DetailRow, { label: 'Distinct subcontractors active',
          value: p.distinct_subs }),
        React.createElement(DetailRow, { label: 'Action completion',
          value: p.action_total > 0
            ? Math.round((p.action_done / p.action_total) * 100) + '% (' + p.action_done + '/' + p.action_total + ')'
            : 'No actions tracked' }),
        React.createElement(DetailRow, { label: 'Overdue actions',
          value: p.action_overdue }),
      ),
      /* Drill-down link to /insights filtered to this project */
      React.createElement('div', { className: 'fs-strategic-detail__actions' },
        React.createElement('button', {
          type: 'button',
          className: 'fs-strategic-detail__action-btn',
          onClick: function () { window.FS.Router.navigate('/insights'); },
        }, 'Open insights →'),
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
  window.FieldSight.PAGES['/portfolio'] = {
    Provider: Provider,
    Middle:   MiddleColumn,
    Right:    RightDetail,
    layout:   'full-width',   /* Sprint 9.5.1 — 2-panel canvas, drill-down via RightDrawer */
  };

})();
