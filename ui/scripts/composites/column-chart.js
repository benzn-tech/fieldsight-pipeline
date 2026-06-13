/* ==========================================================================
   FieldSight ColumnChart — Layer 5 composite (Sprint 9.5.3)
   --------------------------------------------------------------------------
   Vertical bar chart. Each bar carries one categorical value; bars
   can stack tone-segmented sub-values. Used by /portfolio · /regional
   · /executive to render distributions (e.g., projects per health
   grade, incidents per week) where individual bar heights matter
   more than the trend curve a sparkline conveys.

   Vanilla SVG; no library. Tone-aware so dark / light themes both
   render correctly.

   Props:
     data       [{ label, value, segments?: [{ value, tone }],
                   meta?: string, selected?: boolean }]
     height     px (default 160)
     onSelect   (row) => void  — click handler per bar
     emptyText  string

   Exported to: window.FieldSight.ColumnChart
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  /* Theme-aware chart fill — see tokens.css §"Chart fills". */
  function toneVar(tone) {
    switch (tone) {
      case 'danger':  return 'var(--fs-chart-danger)';
      case 'warning': return 'var(--fs-chart-warning)';
      case 'success': return 'var(--fs-chart-success)';
      case 'info':    return 'var(--fs-chart-info)';
      case 'accent':  return 'var(--fs-chart-accent)';
      default:        return 'var(--border-strong)';
    }
  }

  function ColumnChart(props) {
    var data      = props.data || [];
    var height    = props.height || 160;
    var onSelect  = props.onSelect;
    var emptyText = props.emptyText || 'No data for this range.';

    if (data.length === 0) {
      return React.createElement('div', {
        className: 'fs-column-chart__empty',
        style:     { height: height + 'px' },
      }, emptyText);
    }

    /* Padding accommodates label below + value above without
       clipping. */
    var padTop    = 18;
    var padBottom = 22;
    var plotH     = height - padTop - padBottom;
    var maxV = data.reduce(function (m, r) {
      return (r.value || 0) > m ? (r.value || 0) : m;
    }, 1);

    return React.createElement('div', {
      className: 'fs-column-chart',
      style:     { height: height + 'px' },
    },
      data.map(function (row, i) {
        var clickable = !!onSelect;
        var pct = ((row.value || 0) / maxV);
        var barH = Math.max(2, plotH * pct);

        var segs = (row.segments && row.segments.length > 0)
          ? row.segments
          : [{ value: row.value || 0, tone: row.tone || 'accent' }];
        var totalSeg = segs.reduce(function (s, x) { return s + (x.value || 0); }, 0)
          || (row.value || 1);

        return React.createElement('div', {
          key:       row.key || i,
          className: 'fs-column-chart__col'
            + (clickable ? ' fs-column-chart__col--clickable' : '')
            + (row.selected ? ' fs-column-chart__col--selected' : ''),
          onClick:   clickable ? function () { onSelect(row); } : undefined,
          tabIndex:  clickable ? 0 : undefined,
          role:      clickable ? 'button' : undefined,
          onKeyDown: clickable ? function (e) {
            if (e.key === 'Enter' || e.key === ' ') {
              e.preventDefault();
              onSelect(row);
            }
          } : undefined,
          title:     row.label + ': ' + (row.value || 0),
        },
          /* Numeric value above the bar */
          React.createElement('div', {
            className: 'fs-column-chart__value',
            style:     { height: padTop + 'px' },
          }, (row.value || 0).toLocaleString()),
          /* The bar itself — flex column so segments stack
             bottom-up. */
          React.createElement('div', {
            className: 'fs-column-chart__bar-wrap',
            style:     { height: plotH + 'px' },
          },
            React.createElement('div', {
              className: 'fs-column-chart__bar',
              style:     { height: barH + 'px' },
            },
              segs.map(function (s, j) {
                var segPct = ((s.value || 0) / totalSeg) * 100;
                return React.createElement('span', {
                  key:       j,
                  className: 'fs-column-chart__seg',
                  style: {
                    height:     segPct + '%',
                    background: toneVar(s.tone),
                  },
                  title: s.label || (s.tone + ': ' + s.value),
                });
              }),
            ),
          ),
          /* Label under the bar */
          React.createElement('div', {
            className: 'fs-column-chart__label',
            style:     { height: padBottom + 'px' },
          }, row.label),
        );
      }),
    );
  }

  if (!window.FieldSight) window.FieldSight = {};
  window.FieldSight.ColumnChart = ColumnChart;

})();
