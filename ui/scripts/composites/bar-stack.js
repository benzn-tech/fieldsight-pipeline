/* ==========================================================================
   FieldSight BarStack — Layer 5 composite (Sprint 9 Track A.2)
   --------------------------------------------------------------------------
   Horizontal stacked bar list. Each row carries a label + N segments,
   each segment a value + tone. Used by /insights to render top-N
   subcontractors and top-N tags with risk-level segmentation
   (high / medium / low) inline on the bar.

   Vanilla SVG only — no chart library, no build step. Tones map to the
   existing --color-{tone}-{300,500} tokens so dark mode just works
   (Sprint 7 / 8 colour-pinning patterns).

   Props:
     data       [{ label: string, value: number, segments: [{value, tone}],
                   meta: string?, selected: boolean? }]
     max        number — max value used to scale bar widths (defaults to
                  the highest data[].value if omitted)
     onSelect   (row) => void — click handler per row
     emptyText  string — fallback when data.length === 0

   Exported to: window.FieldSight.BarStack
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  /* Tone slug → theme-aware chart fill (deeper in light mode,
     softer in dark; see tokens.css §"Chart fills + per-tag palette"). */
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

  function BarStack(props) {
    var data      = props.data || [];
    var onSelect  = props.onSelect;
    var emptyText = props.emptyText || 'No data for this range.';

    if (data.length === 0) {
      return React.createElement('div', { className: 'fs-bar-stack__empty' },
        emptyText);
    }

    var explicitMax = props.max;
    var max = (explicitMax != null && explicitMax > 0)
      ? explicitMax
      : Math.max.apply(null, data.map(function (r) { return r.value || 0; })) || 1;

    return React.createElement('ul', { className: 'fs-bar-stack', role: 'list' },
      data.map(function (row, i) {
        var pct = Math.min(100, ((row.value || 0) / max) * 100);
        var clickable = !!onSelect;

        /* If no segments, render a single block in the row's tone (or
           neutral). When segments exist they sum to row.value and we
           render them stacked left-to-right. */
        var segs = (row.segments && row.segments.length > 0)
          ? row.segments
          : [{ value: row.value || 0, tone: row.tone || 'accent' }];

        var totalSeg = segs.reduce(function (s, x) { return s + (x.value || 0); }, 0)
          || (row.value || 1);

        return React.createElement('li', {
          key: row.key || i,
          className: 'fs-bar-stack__row'
            + (clickable ? ' fs-bar-stack__row--clickable' : '')
            + (row.selected ? ' fs-bar-stack__row--selected' : ''),
          onClick: clickable ? function () { onSelect(row); } : undefined,
          tabIndex: clickable ? 0 : undefined,
          role: clickable ? 'button' : undefined,
          onKeyDown: clickable ? function (e) {
            if (e.key === 'Enter' || e.key === ' ') {
              e.preventDefault();
              onSelect(row);
            }
          } : undefined,
        },
          React.createElement('div', { className: 'fs-bar-stack__label' },
            React.createElement('span', { className: 'fs-bar-stack__name' }, row.label),
            row.meta
              ? React.createElement('span', { className: 'fs-bar-stack__meta' }, row.meta)
              : null,
          ),
          React.createElement('div', { className: 'fs-bar-stack__track' },
            React.createElement('div', {
              className: 'fs-bar-stack__bar',
              style: { width: pct + '%' },
            },
              segs.map(function (s, j) {
                var segPct = ((s.value || 0) / totalSeg) * 100;
                /* Sprint 9.5.7 — explicit segment.color wins over
                   the tone-based fallback. Used by /insights's
                   TopTags so each tag bar paints in its own
                   categorical hue from the new --fs-tag-{slug}
                   palette. */
                var fill = s.color || toneVar(s.tone);
                return React.createElement('span', {
                  key:   j,
                  className: 'fs-bar-stack__seg',
                  style: {
                    width:      segPct + '%',
                    background: fill,
                  },
                  title: s.label || (s.tone + ': ' + s.value),
                });
              }),
            ),
          ),
          React.createElement('div', { className: 'fs-bar-stack__value' },
            (row.value || 0).toLocaleString()),
        );
      }),
    );
  }

  if (!window.FieldSight) window.FieldSight = {};
  window.FieldSight.BarStack = BarStack;

})();
