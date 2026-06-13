/* ==========================================================================
   FieldSight RollupTable — Layer 5 composite (Sprint 9 Track C.2)
   --------------------------------------------------------------------------
   Sortable per-row table for strategic dashboards. Each row carries
   one entity (project / region) with several metric columns. A
   trailing trend column reuses the SparkLine composite so PMs can
   spot direction at a glance without leaving the row.

   Vanilla SVG via SparkLine; no chart library. Sorting is purely
   client-side, in-memory.

   Props:
     columns   [{ key, label, type, sortKey?, render?(row), align? }]
                  type: 'text' | 'num' | 'percent' | 'health' | 'trend' | 'currency'
     rows      [{ id, ...metric fields, trend?: [{date, value}] }]
     onSelect  (row) => void
     selectedId    string — match against row.id to highlight

   Exported to: window.FieldSight.RollupTable
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  function fmtPercent(v) {
    if (v == null) return '—';
    return Math.round(v * 100) + '%';
  }

  function fmtCurrency(v) {
    if (!v) return '—';
    /* Compact NZD: $12.4M / $890k. */
    if (v >= 1_000_000) return '$' + (v / 1_000_000).toFixed(1) + 'M';
    if (v >= 1_000)     return '$' + (v / 1_000).toFixed(0) + 'k';
    return '$' + v.toLocaleString();
  }

  function fmtNum(v) {
    if (v == null) return '—';
    return v.toLocaleString();
  }

  function defaultSortKey(row, col) {
    var raw = row[col.key];
    /* Health grades sort D < C < B < A so D appears first when desc. */
    if (col.type === 'health') {
      var GRADE_ORDER = { D: 0, C: 1, B: 2, A: 3 };
      return GRADE_ORDER[raw] != null ? GRADE_ORDER[raw] : 99;
    }
    return raw;
  }

  function RollupTable(props) {
    var fs        = window.FieldSight;
    var SparkLine = fs.SparkLine;
    var HealthScore = fs.HealthScore;

    var columns  = props.columns  || [];
    var rows     = props.rows     || [];
    var onSelect = props.onSelect;
    var selectedId = props.selectedId;

    var refSort = React.useState({
      key:       (columns.filter(function (c) { return c.type === 'health'; })[0] || columns[0] || {}).key,
      direction: 'asc',
    });
    var sort    = refSort[0];
    var setSort = refSort[1];

    function onHeaderClick(col) {
      setSort(function (prev) {
        if (prev.key === col.key) {
          return { key: col.key, direction: prev.direction === 'asc' ? 'desc' : 'asc' };
        }
        return { key: col.key, direction: 'asc' };
      });
    }

    /* Sorted rows (immutable). */
    var sortCol = columns.filter(function (c) { return c.key === sort.key; })[0];
    var sortedRows = sortCol
      ? rows.slice().sort(function (a, b) {
          var av = (sortCol.sortKey ? sortCol.sortKey(a) : defaultSortKey(a, sortCol));
          var bv = (sortCol.sortKey ? sortCol.sortKey(b) : defaultSortKey(b, sortCol));
          if (av == null) av = sort.direction === 'asc' ?  Infinity : -Infinity;
          if (bv == null) bv = sort.direction === 'asc' ?  Infinity : -Infinity;
          if (av < bv) return sort.direction === 'asc' ? -1 :  1;
          if (av > bv) return sort.direction === 'asc' ?  1 : -1;
          return 0;
        })
      : rows;

    function renderCell(row, col) {
      if (col.render) return col.render(row);
      var raw = row[col.key];
      switch (col.type) {
        case 'percent':  return fmtPercent(raw);
        case 'currency': return fmtCurrency(raw);
        case 'num':      return fmtNum(raw);
        case 'health':
          return HealthScore
            ? React.createElement(HealthScore, { grade: raw, size: 'sm' })
            : (raw || '—');
        case 'trend':
          if (!SparkLine || !raw || raw.length === 0) {
            return React.createElement('span', { className: 'fs-rollup-table__cell-empty' }, '—');
          }
          return React.createElement(SparkLine, {
            points: raw, tone: col.tone || 'danger',
            width: 100, height: 28,
          });
        default:
          return raw != null ? raw : '—';
      }
    }

    return React.createElement('table', {
      className: 'fs-rollup-table',
      role:      'table',
    },
      React.createElement('thead', null,
        React.createElement('tr', null,
          columns.map(function (col) {
            var isSorted   = sort.key === col.key;
            var sortable   = col.type !== 'trend';
            return React.createElement('th', {
              key:       col.key,
              scope:     'col',
              className: 'fs-rollup-table__th'
                + (col.align ? ' fs-rollup-table__th--' + col.align : '')
                + (sortable ? ' fs-rollup-table__th--sortable' : '')
                + (isSorted ? ' fs-rollup-table__th--sorted-' + sort.direction : ''),
              onClick:   sortable ? function () { onHeaderClick(col); } : undefined,
              tabIndex:  sortable ? 0 : undefined,
              role:      sortable ? 'button' : undefined,
              onKeyDown: sortable ? function (e) {
                if (e.key === 'Enter' || e.key === ' ') {
                  e.preventDefault();
                  onHeaderClick(col);
                }
              } : undefined,
              'aria-sort': isSorted
                ? (sort.direction === 'asc' ? 'ascending' : 'descending')
                : undefined,
            },
              col.label,
              isSorted
                ? React.createElement('span', { className: 'fs-rollup-table__sort-arrow', 'aria-hidden': true },
                    sort.direction === 'asc' ? ' ↑' : ' ↓')
                : null,
            );
          }),
        ),
      ),
      React.createElement('tbody', null,
        sortedRows.length === 0
          ? React.createElement('tr', null,
              React.createElement('td', {
                colSpan:   columns.length,
                className: 'fs-rollup-table__empty',
              }, 'No data for this range.'))
          : sortedRows.map(function (row) {
              var clickable = !!onSelect;
              var sel = row.id === selectedId;
              return React.createElement('tr', {
                key:       row.id,
                className: 'fs-rollup-table__row'
                  + (clickable ? ' fs-rollup-table__row--clickable' : '')
                  + (sel ? ' fs-rollup-table__row--selected' : ''),
                onClick:   clickable ? function () { onSelect(row); } : undefined,
                tabIndex:  clickable ? 0 : undefined,
                role:      clickable ? 'button' : undefined,
                onKeyDown: clickable ? function (e) {
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    onSelect(row);
                  }
                } : undefined,
              },
                columns.map(function (col) {
                  return React.createElement('td', {
                    key:       col.key,
                    className: 'fs-rollup-table__td'
                      + (col.align ? ' fs-rollup-table__td--' + col.align : ''),
                  }, renderCell(row, col));
                }),
              );
            }),
      ),
    );
  }

  if (!window.FieldSight) window.FieldSight = {};
  window.FieldSight.RollupTable = RollupTable;

})();
