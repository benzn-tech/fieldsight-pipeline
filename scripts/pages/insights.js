/* ==========================================================================
   FieldSight /insights — Sprint 9 Track A.1
   --------------------------------------------------------------------------
   PM-facing analytics dashboard. Aggregates safety + quality issues
   over a date window (default last 7 days, switchable to 30) and
   surfaces three focusable views:

     • KPI strip — totals + week-over-week deltas
     • Top-5 subcontractors (with risk-level segmentation per bar)
     • Top-5 tags (with linked top-subcontractor caption)
     • 14-day daily-count sparkline
     • Drill-down: clicking a sub or tag filters the row list below
                   and pivots the right detail to that profile

   No new backend endpoints; reuses compliance-aggregator + the new
   insights-aggregator (Sprint 9 A.0/A.1). Permission gate:
   `insights:view` (added to roles.js for SM, PM, HSE, QC, gm,
   director, admin in this sprint).

   Registers as window.FieldSight.PAGES['/insights'].
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  /* ─── Helpers ────────────────────────────────────────────────────── */

  function fmtDateLabel(yyyymmdd) {
    if (!yyyymmdd) return '';
    var p = yyyymmdd.split('-').map(Number);
    var d = new Date(Date.UTC(p[0], p[1] - 1, p[2]));
    var months = ['Jan','Feb','Mar','Apr','May','Jun',
                  'Jul','Aug','Sep','Oct','Nov','Dec'];
    return d.getUTCDate() + ' ' + months[d.getUTCMonth()];
  }

  function rangeFor(mode, today) {
    /* mode = 'week' → last 7 days incl today; 'month' → last 30 days */
    var span = mode === 'month' ? 29 : 6;
    return {
      from: window.FS.api.addDaysISO(today, -span),
      to:   today,
    };
  }

  function priorRangeFor(mode, today) {
    /* The matched-length window immediately before the current one,
       used for week-over-week / month-over-month deltas. */
    var span = mode === 'month' ? 30 : 7;
    return {
      from: window.FS.api.addDaysISO(today, -(span * 2 - 1)),
      to:   window.FS.api.addDaysISO(today, -span),
    };
  }

  /* ─── Provider ───────────────────────────────────────────────────── */

  var InsightsContext = React.createContext(null);

  function InsightsProvider(props) {
    var caller = (window.AuthMock && window.AuthMock.currentUser) || {};

    /* Permission gate (matches NAV_ITEMS gating in fs-globals.js,
       defense in depth: a direct URL hit lands here too). */
    var canSee = window.FS && window.FS.can && window.FS.can(caller, 'insights:view');

    var refMode = React.useState('week');
    var mode = refMode[0]; var setMode = refMode[1];

    var refSel = React.useState(null);    /* { kind: 'sub'|'tag', id } */
    var sel = refSel[0]; var setSel = refSel[1];

    var refState = React.useState({ status: canSee ? 'loading' : 'access_denied' });
    var state = refState[0]; var setState = refState[1];

    var retryRef   = React.useState(0);
    var retryCount = retryRef[0];
    var setRetry   = retryRef[1];

    React.useEffect(function () {
      if (!canSee) {
        setState({ status: 'access_denied', message: 'Insights requires the insights:view permission.' });
        return undefined;
      }
      var cancelled = false;
      setState({ status: 'loading' });

      var today    = window.FS.api.todayNZDT();
      var current  = rangeFor(mode, today);
      var previous = priorRangeFor(mode, today);

      Promise.all([
        window.FS.api.insights.getInsights({
          from: current.from,  to: current.to,  kind: 'all',
        }),
        window.FS.api.insights.getInsights({
          from: previous.from, to: previous.to, kind: 'all',
        }),
      ]).then(function (results) {
        if (cancelled) return;
        var cur  = results[0];
        var prev = results[1];

        if ((cur.safety && cur.safety._accessDenied) ||
            (cur.quality && cur.quality._accessDenied)) {
          var denied = (cur.safety && cur.safety._accessDenied) ? cur.safety : cur.quality;
          setState({ status: 'access_denied', message: denied.error });
          return;
        }

        setState({
          status:   'ok',
          range:    { mode: mode, current: current, previous: previous, today: today },
          safety:   cur.safety  || emptySection(),
          quality:  cur.quality || emptySection(),
          previous: {
            safety:  (prev.safety  && !prev.safety._accessDenied)  ? prev.safety  : emptySection(),
            quality: (prev.quality && !prev.quality._accessDenied) ? prev.quality : emptySection(),
          },
        });
      }).catch(function (err) {
        if (cancelled) return;
        setState({
          status: 'error',
          error: {
            code:      (err && err.status)  || 0,
            message:   (err && err.message) || 'Could not load insights',
            retryable: true,
          },
          retry:  function () { setRetry(function (n) { return n + 1; }); },
        });
      });

      return function () { cancelled = true; };
    }, [mode, retryCount, canSee]);

    var ctx = {
      state:      state,
      mode:       mode,
      setMode:    function (m) { setSel(null); setMode(m); },
      selection:  sel,
      setSelection: setSel,
      caller:     caller,
    };

    return React.createElement(InsightsContext.Provider, { value: ctx },
      props.children);
  }

  function emptySection() {
    return { rows: [], bySub: [], byTag: [], byDay: [], totals: { count: 0, high: 0, medium: 0, low: 0, distinct_subs: 0, distinct_tags: 0 } };
  }

  /* ─── Range toolbar ──────────────────────────────────────────────── */

  function RangeToolbar(props) {
    var ctx = props.ctx;
    var modes = [
      { value: 'week',  label: 'Last 7 days'  },
      { value: 'month', label: 'Last 30 days' },
    ];
    return React.createElement('div', { className: 'fs-insights__toolbar', role: 'group', 'aria-label': 'Date range' },
      modes.map(function (m) {
        var active = ctx.mode === m.value;
        return React.createElement('button', {
          key: m.value,
          type: 'button',
          className: 'fs-insights__chip' + (active ? ' fs-insights__chip--active' : ''),
          onClick: function () { ctx.setMode(m.value); },
        }, m.label);
      }),
    );
  }

  /* ─── KPI strip ──────────────────────────────────────────────────── */

  function InsightsKpis(props) {
    var fs       = window.FieldSight;
    var KpiStrip = fs.KpiStrip;
    var StatCard = fs.StatCard;
    var TrendPill= fs.TrendPill;

    var safety       = props.safety;
    var quality      = props.quality;
    var prevSafety   = props.prevSafety;
    var prevQuality  = props.prevQuality;

    var safetyDelta  = safety.totals.count  - prevSafety.totals.count;
    var qualityDelta = quality.totals.count - prevQuality.totals.count;

    var topSub = safety.bySub[0] || quality.bySub[0] || null;
    var topTag = safety.byTag[0] || quality.byTag[0] || null;

    return React.createElement(KpiStrip, null,
      React.createElement(StatCard, {
        value: safety.totals.count, label: 'Safety issues',
        tone: safety.totals.high > 0 ? 'danger' : 'neutral',
        footer: TrendPill ? React.createElement(TrendPill, {
          delta: safetyDelta, unit: '', polarity: 'lower_better',
        }) : null,
      }),
      React.createElement(StatCard, {
        value: quality.totals.count, label: 'Quality issues',
        tone: quality.totals.count > 0 ? 'warning' : 'neutral',
        footer: TrendPill ? React.createElement(TrendPill, {
          delta: qualityDelta, unit: '', polarity: 'lower_better',
        }) : null,
      }),
      React.createElement(StatCard, {
        value: topSub ? topSub.name : '—',
        label: topSub ? 'Top subcontractor (' + topSub.count + ')' : 'Top subcontractor',
        tone:  'neutral',
      }),
      React.createElement(StatCard, {
        value: topTag ? topTag.label : '—',
        label: topTag ? 'Top tag (' + topTag.count + ')' : 'Top tag',
        tone:  'neutral',
      }),
    );
  }

  /* ─── Section · Top-N subcontractors ─────────────────────────────── */

  function TopSubcontractors(props) {
    var fs       = window.FieldSight;
    var BarStack = fs.BarStack;
    var ctx      = props.ctx;
    var section  = props.section;       /* safety or quality slice */
    var kindLabel = props.kindLabel;
    if (!BarStack) return null;

    /* Compose data for the bar stack. Risk-level segmentation lets
       PMs see at-a-glance how dangerous each sub's issues are. */
    var data = section.bySub.slice(0, 5).map(function (b) {
      return {
        key:      b.subcontractor_id,
        label:    b.name,
        meta:     b.trade ? b.trade : null,
        value:    b.count,
        selected: ctx.selection && ctx.selection.kind === 'sub'
                  && ctx.selection.id === b.subcontractor_id,
        segments: [
          { value: b.high,   tone: 'danger',  label: 'High: '   + b.high   },
          { value: b.medium, tone: 'warning', label: 'Medium: ' + b.medium },
          { value: b.low,    tone: 'success', label: 'Low: '    + b.low    },
        ].filter(function (s) { return s.value > 0; }),
      };
    });

    return React.createElement('section', { className: 'fs-insights__section' },
      React.createElement('div', { className: 'fs-insights__section-header' },
        React.createElement('h3', { className: 'fs-insights__section-title' },
          'Top subcontractors · ' + kindLabel),
        React.createElement('div', { className: 'fs-insights__section-meta' },
          section.totals.distinct_subs + ' distinct'),
      ),
      React.createElement(BarStack, {
        data:     data,
        emptyText: 'No ' + kindLabel.toLowerCase() + ' issues attributed to a subcontractor in this range.',
        onSelect: function (row) {
          if (!row.key || row.key === 'unknown') return;
          ctx.setSelection({ kind: 'sub', id: row.key });
        },
      }),
    );
  }

  /* ─── Section · Top-N tags ───────────────────────────────────────── */

  function TopTags(props) {
    var fs       = window.FieldSight;
    var BarStack = fs.BarStack;
    var ins      = window.FS && window.FS.insights;
    var ctx      = props.ctx;
    var section  = props.section;
    var kindLabel = props.kindLabel;
    if (!BarStack) return null;

    var data = section.byTag.slice(0, 5).map(function (b) {
      var topSub = b.top_subcontractor && ins
        ? ins.subcontractorById(b.top_subcontractor)
        : null;
      var meta = topSub ? 'Most-often: ' + topSub.name : null;
      var voc  = (ins ? ins.TAG_VOCAB : []).filter(function (v) { return v.slug === b.tag; })[0];
      var tone = voc ? voc.tone : 'accent';
      return {
        key:      b.tag,
        label:    b.label,
        meta:     meta,
        value:    b.count,
        tone:     tone,
        selected: ctx.selection && ctx.selection.kind === 'tag'
                  && ctx.selection.id === b.tag,
        segments: [{ value: b.count, tone: tone }],
      };
    });

    return React.createElement('section', { className: 'fs-insights__section' },
      React.createElement('div', { className: 'fs-insights__section-header' },
        React.createElement('h3', { className: 'fs-insights__section-title' },
          'Top tags · ' + kindLabel),
        React.createElement('div', { className: 'fs-insights__section-meta' },
          section.totals.distinct_tags + ' distinct'),
      ),
      React.createElement(BarStack, {
        data:     data,
        emptyText: 'No tagged ' + kindLabel.toLowerCase() + ' issues in this range.',
        onSelect: function (row) {
          ctx.setSelection({ kind: 'tag', id: row.key });
        },
      }),
    );
  }

  /* ─── Section · Daily trend sparkline ────────────────────────────── */

  function DailyTrend(props) {
    var fs        = window.FieldSight;
    var SparkLine = fs.SparkLine;
    var section   = props.section;
    var kindLabel = props.kindLabel;
    if (!SparkLine) return null;

    var points = section.byDay.map(function (d) {
      return { date: d.date, value: d.count, label: fmtDateLabel(d.date) };
    });

    return React.createElement('section', { className: 'fs-insights__section fs-insights__section--inline' },
      React.createElement('div', { className: 'fs-insights__inline-label' }, kindLabel + ' trend'),
      React.createElement(SparkLine, {
        points: points, tone: kindLabel === 'Quality' ? 'info' : 'danger',
        width: 280, height: 48, showLastValue: true,
      }),
    );
  }

  /* ─── Section · Drill-down rows ──────────────────────────────────── */

  function DrillDown(props) {
    var fs        = window.FieldSight;
    var Card      = fs.Card;
    var Badge     = fs.Badge;
    var ctx       = props.ctx;
    var safety    = props.safety;
    var quality   = props.quality;
    var sel       = ctx.selection;
    if (!sel) return null;

    function matches(r) {
      if (sel.kind === 'sub') return r.subcontractor_id === sel.id;
      if (sel.kind === 'tag') return (r.tags || []).indexOf(sel.id) >= 0;
      return false;
    }
    var rows = []
      .concat((safety.rows  || []).filter(matches).map(function (r) { return Object.assign({ _kind: 'safety'  }, r); }))
      .concat((quality.rows || []).filter(matches).map(function (r) { return Object.assign({ _kind: 'quality' }, r); }))
      .sort(function (a, b) { return a.date < b.date ? 1 : -1; });

    var title = sel.kind === 'sub'
      ? ('Issues attributed to ' + (rows[0] && rows[0].subcontractor_id ? '' : '') + (sel.id || ''))
      : ('Issues tagged ' + sel.id);

    return React.createElement('section', { className: 'fs-insights__section' },
      React.createElement('div', { className: 'fs-insights__section-header' },
        React.createElement('h3', { className: 'fs-insights__section-title' },
          rows.length + ' matching issue' + (rows.length === 1 ? '' : 's')),
        React.createElement('button', {
          type: 'button',
          className: 'fs-insights__clear',
          onClick: function () { ctx.setSelection(null); },
        }, 'Clear filter'),
      ),
      rows.length === 0
        ? React.createElement('div', { className: 'fs-insights__empty' },
            'No matches in the current range.')
        : React.createElement('ul', { className: 'fs-insights__rows', role: 'list' },
            rows.map(function (r) {
              return React.createElement('li', {
                key:        r.id,
                className:  'fs-insights__row fs-insights__row--' + r._kind,
              },
                React.createElement('div', { className: 'fs-insights__row-meta' },
                  React.createElement(Badge, {
                    tone: r._kind === 'safety' ? 'danger' : 'info', size: 'sm',
                  }, r._kind === 'safety' ? 'Safety' : 'Quality'),
                  r.risk_level
                    ? React.createElement(Badge, { tone: 'neutral', size: 'sm', variant: 'outline' },
                        r.risk_level)
                    : null,
                  React.createElement('span', { className: 'fs-insights__row-date' },
                    fmtDateLabel(r.date)),
                ),
                React.createElement('div', { className: 'fs-insights__row-text' },
                  r.observation || r.item || '(no description)'),
                r.user_name ? React.createElement('div', { className: 'fs-insights__row-attrib' },
                  'Raised by ' + (r.who_raised || r.user_name)
                  + (r.site ? ' · ' + r.site : '')) : null,
              );
            }),
          ),
    );
  }

  /* ─── Middle column ──────────────────────────────────────────────── */

  function InsightsMiddleColumn() {
    var fs           = window.FieldSight;
    var AccessDenied = fs.AccessDenied;
    var ErrorBanner  = fs.ErrorBanner;

    var ctx = React.useContext(InsightsContext);
    if (!ctx) return null;

    var state = ctx.state;
    var header = React.createElement('div', { className: 'fs-insights__header' },
      React.createElement('h2', { className: 'fs-insights__title' }, 'Insights'),
      React.createElement('div', { className: 'fs-insights__subtitle' },
        'Cross-report safety + quality patterns. Closed 12-tag vocab.'),
    );

    if (state.status === 'access_denied') {
      return React.createElement('div', { className: 'fs-insights' },
        header,
        AccessDenied
          ? React.createElement(AccessDenied, { scope: 'insights', message: state.message })
          : React.createElement('div', { className: 'fs-insights__empty' }, state.message),
      );
    }

    if (state.status === 'loading') {
      return React.createElement('div', { className: 'fs-insights' },
        header,
        React.createElement(RangeToolbar, { ctx: ctx }),
        React.createElement('div', { className: 'fs-insights__loading' }, 'Aggregating…'),
      );
    }

    if (state.status === 'error') {
      return React.createElement('div', { className: 'fs-insights' },
        header,
        React.createElement(RangeToolbar, { ctx: ctx }),
        ErrorBanner ? React.createElement(ErrorBanner, {
          message:   (state.error && state.error.message) || 'Could not load insights',
          retryable: true,
          onRetry:   state.retry,
        }) : null,
      );
    }

    var safety      = state.safety;
    var quality     = state.quality;
    var prevSafety  = state.previous.safety;
    var prevQuality = state.previous.quality;

    return React.createElement('div', { className: 'fs-insights' },
      header,
      React.createElement(RangeToolbar, { ctx: ctx }),
      React.createElement(InsightsKpis, {
        safety:      safety,      quality:      quality,
        prevSafety:  prevSafety,  prevQuality:  prevQuality,
      }),
      /* Trend sparklines side-by-side */
      React.createElement('div', { className: 'fs-insights__trend-row' },
        React.createElement(DailyTrend, { section: safety,  kindLabel: 'Safety'  }),
        React.createElement(DailyTrend, { section: quality, kindLabel: 'Quality' }),
      ),
      /* Top-5 subcontractors — safety only by default; quality follows */
      React.createElement(TopSubcontractors, {
        ctx: ctx, section: safety,  kindLabel: 'Safety',
      }),
      React.createElement(TopTags, {
        ctx: ctx, section: safety,  kindLabel: 'Safety',
      }),
      React.createElement(TopSubcontractors, {
        ctx: ctx, section: quality, kindLabel: 'Quality',
      }),
      React.createElement(TopTags, {
        ctx: ctx, section: quality, kindLabel: 'Quality',
      }),
      /* Drill-down (only when something is selected) */
      React.createElement(DrillDown, {
        ctx: ctx, safety: safety, quality: quality,
      }),
    );
  }

  /* ─── Right detail · selected sub or tag profile ─────────────────── */

  function InsightsRightDetail() {
    var fs         = window.FieldSight;
    var Card       = fs.Card;
    var SparkLine  = fs.SparkLine;
    var ins        = window.FS && window.FS.insights;
    var ctx        = React.useContext(InsightsContext);
    if (!ctx) return null;

    var sel = ctx.selection;
    var state = ctx.state;

    if (!sel || state.status !== 'ok') {
      return React.createElement('div', { className: 'fs-insights-detail__placeholder' },
        React.createElement('div', { className: 'fs-insights-detail__placeholder-title' },
          'Select a subcontractor or tag'),
        React.createElement('div', { className: 'fs-insights-detail__placeholder-body' },
          'Pick any bar in the lists to see its full profile, trend, and matching issues.'),
      );
    }

    if (sel.kind === 'sub') {
      var sub = ins ? ins.subcontractorById(sel.id) : null;
      var safetyBucket  = (state.safety.bySub  || []).filter(function (b) { return b.subcontractor_id === sel.id; })[0];
      var qualityBucket = (state.quality.bySub || []).filter(function (b) { return b.subcontractor_id === sel.id; })[0];

      /* Build a per-day series for this sub by walking byDay totals
         and counting our rows on each date. */
      var subRows = [].concat(safetyBucket ? safetyBucket.rows : [])
                       .concat(qualityBucket ? qualityBucket.rows : []);
      var subPoints = state.safety.byDay.map(function (d) {
        var n = subRows.filter(function (r) { return r.date === d.date; }).length;
        return { date: d.date, value: n };
      });

      return React.createElement('div', { className: 'fs-insights-detail' },
        React.createElement('div', { className: 'fs-insights-detail__header' },
          React.createElement('h2', { className: 'fs-insights-detail__name' },
            sub ? sub.name : 'Unattributed'),
          sub ? React.createElement('div', { className: 'fs-insights-detail__sub' },
            sub.trade) : null,
        ),
        SparkLine ? React.createElement(SparkLine, {
          points: subPoints, tone: 'danger', width: 320, height: 56,
        }) : null,
        React.createElement('div', { className: 'fs-insights-detail__rows' },
          React.createElement(DetailRow, { label: 'Safety issues',
            value: safetyBucket ? safetyBucket.count.toString() : '0' }),
          React.createElement(DetailRow, { label: 'High-risk safety',
            value: safetyBucket ? safetyBucket.high.toString() : '0' }),
          React.createElement(DetailRow, { label: 'Quality issues',
            value: qualityBucket ? qualityBucket.count.toString() : '0' }),
          sub ? React.createElement(DetailRow, { label: 'Sites',
            value: (sub.sites || []).join(', ') }) : null,
        ),
      );
    }

    if (sel.kind === 'tag') {
      var voc = (ins ? ins.TAG_VOCAB : []).filter(function (v) { return v.slug === sel.id; })[0];
      var tagSafety  = (state.safety.byTag  || []).filter(function (b) { return b.tag === sel.id; })[0];
      var tagQuality = (state.quality.byTag || []).filter(function (b) { return b.tag === sel.id; })[0];

      var tagRows = [].concat(tagSafety  ? tagSafety.rows  : [])
                       .concat(tagQuality ? tagQuality.rows : []);
      var tagPoints = state.safety.byDay.map(function (d) {
        var n = tagRows.filter(function (r) { return r.date === d.date; }).length;
        return { date: d.date, value: n };
      });

      var topSub = (tagSafety && tagSafety.top_subcontractor)
        ? (ins ? ins.subcontractorById(tagSafety.top_subcontractor) : null)
        : null;

      return React.createElement('div', { className: 'fs-insights-detail' },
        React.createElement('div', { className: 'fs-insights-detail__header' },
          React.createElement('h2', { className: 'fs-insights-detail__name' },
            voc ? voc.label : sel.id),
          React.createElement('div', { className: 'fs-insights-detail__sub' },
            'Tag · ' + sel.id),
        ),
        SparkLine ? React.createElement(SparkLine, {
          points: tagPoints, tone: voc ? voc.tone : 'accent',
          width: 320, height: 56,
        }) : null,
        React.createElement('div', { className: 'fs-insights-detail__rows' },
          React.createElement(DetailRow, { label: 'Safety hits',
            value: tagSafety ? tagSafety.count.toString() : '0' }),
          React.createElement(DetailRow, { label: 'Quality hits',
            value: tagQuality ? tagQuality.count.toString() : '0' }),
          React.createElement(DetailRow, { label: 'Most-often subcontractor',
            value: topSub ? topSub.name : '—' }),
        ),
      );
    }

    return null;
  }

  function DetailRow(props) {
    return React.createElement('div', { className: 'fs-insights-detail__row' },
      React.createElement('div', { className: 'fs-insights-detail__row-label' },
        props.label),
      React.createElement('div', { className: 'fs-insights-detail__row-value' },
        props.value),
    );
  }

  /* ─── Register ───────────────────────────────────────────────────── */

  if (!window.FieldSight) window.FieldSight = {};
  if (!window.FieldSight.PAGES) window.FieldSight.PAGES = {};
  window.FieldSight.PAGES['/insights'] = {
    Provider: InsightsProvider,
    Middle:   InsightsMiddleColumn,
    Right:    InsightsRightDetail,
    layout:   'full-width',   /* Sprint 9.5.1 — 2-panel canvas, drill-down via RightDrawer */
  };

})();
