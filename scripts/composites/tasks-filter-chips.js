/* ==========================================================================
   FieldSight TasksFilterChips — Layer 5 composite (Sprint 4.2)
   --------------------------------------------------------------------------
   Filter chips for the /tasks page. Mirrors the visual pattern of
   /reports' filter chips (Sprint 2.6 / Phase F) so the two pages
   feel consistent.

   Filters are mutually exclusive:
     all      — every action across the range
     mine     — responsible === current user's display_name
     open     — audit.checked === false
     overdue  — open AND deadline parses to a date earlier than today
                (deadline is free text in BACKEND-CONTEXT §5.1, so we
                fall back to "no overdue match" when unparseable)
     done     — audit.checked === true

   Counts are computed by the page and passed in as a prop so the
   composite stays purely presentational.

   Props:
     value      string — active filter key
     onChange   (key) => void
     counts     { all, mine, open, overdue, done }

   Exported to:
     window.FieldSight.TasksFilterChips
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  var FILTERS = [
    { key: 'all',     label: 'All' },
    { key: 'mine',    label: 'Mine' },
    { key: 'open',    label: 'Open' },
    { key: 'overdue', label: 'Overdue' },
    { key: 'done',    label: 'Done' },
  ];

  function TasksFilterChips(props) {
    var counts = props.counts || {};
    var value  = props.value || 'all';

    return React.createElement('div', { className: 'fs-tasks__chips' },
      FILTERS.map(function (f) {
        var active = value === f.key;
        return React.createElement('button', {
          key:       f.key,
          type:      'button',
          className: 'fs-tasks__chip'
                     + (active ? ' fs-tasks__chip--active' : '')
                     + (f.key === 'overdue' ? ' fs-tasks__chip--overdue' : ''),
          onClick:   function () { if (props.onChange) props.onChange(f.key); },
          'aria-pressed': active,
        },
          f.label,
          React.createElement('span', { className: 'fs-tasks__chip-count' },
            counts[f.key] != null ? counts[f.key] : 0),
        );
      }),
    );
  }

  if (!window.FieldSight) window.FieldSight = {};
  window.FieldSight.TasksFilterChips = TasksFilterChips;
})();
