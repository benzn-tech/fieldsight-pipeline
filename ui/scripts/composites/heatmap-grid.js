/* ==========================================================================
   FieldSight HeatmapGrid — Layer 5 composite (Sprint 9.5.3)
   --------------------------------------------------------------------------
   Two-dimensional matrix grid. Each cell carries a count; cell fill
   opacity scales with count / maxValue, on top of a per-column tone.
   Used by /insights to surface "Subcontractor X has Y issues in
   tag-category Z" at a glance — answers cross-tabulation questions
   that bar lists cannot.

   CSS-grid layout (no SVG); cells are HTML divs with inline opacity
   + background-color. Theme-token native, dark-mode aware.

   Props:
     rows     [{ id, label, sub?: string }]   — y-axis (subs)
     cols     [{ id, label, color?, tone?  }] — x-axis (tags)
     matrix   { [rowId]: { [colId]: number } } — counts
     onSelect (rowId, colId, count) => void   — click cell
     emptyText string

   Exported to: window.FieldSight.HeatmapGrid
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  /* Per-column fill — prefer explicit `color` (e.g., per-tag hue
     from TAG_VOCAB) and fall back to theme-aware chart tone. */
  function colColor(col) {
    if (col.color) return col.color;
    if (col.tone)  return 'var(--fs-chart-' + col.tone + ')';
    return 'var(--fs-chart-accent)';
  }

  function HeatmapGrid(props) {
    var rows      = props.rows || [];
    var cols      = props.cols || [];
    var matrix    = props.matrix || {};
    var onSelect  = props.onSelect;
    var emptyText = props.emptyText || 'No data for this range.';

    if (rows.length === 0 || cols.length === 0) {
      return React.createElement('div', { className: 'fs-heatmap-grid__empty' },
        emptyText);
    }

    /* Find max count to scale opacity. */
    var maxCount = 0;
    rows.forEach(function (r) {
      cols.forEach(function (c) {
        var v = (matrix[r.id] && matrix[r.id][c.id]) || 0;
        if (v > maxCount) maxCount = v;
      });
    });
    if (maxCount === 0) maxCount = 1;

    /* Grid template: first column = row label (auto), then N
       data columns at 1fr each, then a totals column at auto. */
    var gridTemplate = 'auto repeat(' + cols.length + ', minmax(36px, 1fr)) auto';

    function rowTotal(rowId) {
      return cols.reduce(function (acc, c) {
        return acc + ((matrix[rowId] && matrix[rowId][c.id]) || 0);
      }, 0);
    }

    return React.createElement('div', {
      className: 'fs-heatmap-grid',
      role:      'table',
      'aria-label': 'Subcontractor by tag heatmap',
      style:     { gridTemplateColumns: gridTemplate },
    },
      /* Header row: empty cell + col labels + total label */
      React.createElement('div', { className: 'fs-heatmap-grid__corner', role: 'columnheader' }),
      cols.map(function (col) {
        return React.createElement('div', {
          key:       'col-' + col.id,
          className: 'fs-heatmap-grid__col-label',
          role:      'columnheader',
          title:     col.label,
        }, col.label);
      }),
      React.createElement('div', {
        className: 'fs-heatmap-grid__col-label fs-heatmap-grid__col-label--total',
        role:      'columnheader',
      }, 'Total'),

      /* Data rows */
      rows.map(function (row) {
        return React.createElement(React.Fragment, { key: 'row-' + row.id },
          React.createElement('div', {
            className: 'fs-heatmap-grid__row-label',
            role:      'rowheader',
            title:     row.label,
          },
            React.createElement('span', { className: 'fs-heatmap-grid__row-name' }, row.label),
            row.sub ? React.createElement('span', { className: 'fs-heatmap-grid__row-sub' },
              row.sub) : null,
          ),
          cols.map(function (col) {
            var v = (matrix[row.id] && matrix[row.id][col.id]) || 0;
            var opacity = v === 0 ? 0 : (0.15 + (v / maxCount) * 0.85);
            var clickable = !!onSelect && v > 0;
            return React.createElement('button', {
              key:       'cell-' + row.id + '-' + col.id,
              type:      'button',
              className: 'fs-heatmap-grid__cell'
                + (v === 0 ? ' fs-heatmap-grid__cell--empty' : '')
                + (clickable ? ' fs-heatmap-grid__cell--clickable' : ''),
              style: {
                background: 'color-mix(in srgb, ' + colColor(col) + ' ' + Math.round(opacity * 100) + '%, transparent)',
              },
              role:      'cell',
              title:     row.label + ' · ' + col.label + ': ' + v,
              onClick:   clickable ? function () { onSelect(row.id, col.id, v); } : undefined,
              tabIndex:  clickable ? 0 : -1,
              disabled:  !clickable,
            }, v > 0 ? v : '');
          }),
          React.createElement('div', {
            className: 'fs-heatmap-grid__row-total',
            role:      'cell',
          }, rowTotal(row.id)),
        );
      }),
    );
  }

  if (!window.FieldSight) window.FieldSight = {};
  window.FieldSight.HeatmapGrid = HeatmapGrid;

})();
