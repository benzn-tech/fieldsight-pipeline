/* ==========================================================================
   FieldSight ProgrammeKanbanBoard — Layer 5 composite (Sprint 4.8)
   --------------------------------------------------------------------------
   Replaces ProgrammeTodoList (the bucketed This-Week / Next-Week /
   Later view) with a Jira-style status board:

     ┌──────────────────────────────────────────────────────────────┐
     │ ▼ 1.0 Earthworks & Foundations · 3 tasks                     │
     ├─────────────┬─────────────┬─────────────────────┬────────────┤
     │ Not started │ In progress │ Blocked or Delayed  │ Done       │
     │             │ 1.3 Found.. │                     │ 1.1 Site … │
     │             │ 95% ▌       │                     │ 1.2 Bulk…  │
     │             │ 🔴 Critical │                     │            │
     └─────────────┴─────────────┴─────────────────────┴────────────┘

   Rows = WBS parent groups (always rendered, even if empty in a
   column). Columns are fixed by status. Cards distribute into the
   matching column.

   Each card is a clickable button that selects the task; the page
   wires selection through to the RightDrawer (Sprint 4.7).

   Props:
     parents      [{ task_id, wbs, name }, ...]
     leaves       [...] (full task objects)
     today        ISO 'YYYY-MM-DD'
     selectedId   string | null
     criticalSet  Set<task_id>
     onSelect     (task) => void
     collapsedSet Set<group_task_id>  — shared with Gantt (toggle reuse)
     onToggleGroup (groupId) => void

   Exported to:
     window.FieldSight.ProgrammeKanbanBoard
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  /* Status → column key + display label.  Blocked + Delayed share
     the same column so the board stays a tidy 4 wide. */
  var COLUMNS = [
    { key: 'not_started', label: 'Not started', tone: 'neutral' },
    { key: 'in_progress', label: 'In progress', tone: 'info' },
    { key: 'blocked',     label: 'Blocked or Delayed', tone: 'warning' },
    { key: 'done',        label: 'Done',        tone: 'success' },
  ];

  function statusToColumn(status) {
    if (status === 'completed')                      return 'done';
    if (status === 'in_progress')                    return 'in_progress';
    if (status === 'blocked' || status === 'delayed') return 'blocked';
    return 'not_started';
  }

  function fmtRange(start, end) {
    if (!start || !end) return '';
    var p1 = start.split('-').map(Number);
    var p2 = end.split('-').map(Number);
    var months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    return p1[2] + ' ' + months[p1[1] - 1]
        + ' → ' + p2[2] + ' ' + months[p2[1] - 1];
  }

  function shortName(folder) {
    return (folder || '').replace(/_/g, ' ');
  }

  function ProgrammeKanbanBoard(props) {
    var fs       = window.FieldSight;
    var Avatar   = fs.Avatar;
    var Badge    = fs.Badge;

    var parents       = props.parents       || [];
    var leavesAll     = props.leaves        || [];
    var today         = props.today         || '';
    var selectedId    = props.selectedId    || null;
    var criticalSet   = props.criticalSet   || new Set();
    var collapsedSet  = props.collapsedSet  || new Set();
    var onSelect      = props.onSelect      || function () {};
    var onToggleGroup = props.onToggleGroup || function () {};

    /* Sprint 4.10.8 — Mine / All filter chips.
       Worker scope is already enforced server-side by the api module
       (workers see only assigned tasks), so we hide the chip for
       them. site_manager / pm / admin / gm see the full scope and
       use the chip to narrow to just their own assignments. */
    var caller = (window.AuthMock && window.AuthMock.currentUser) || {};
    var canFilter = caller.role !== 'worker';
    var callerFolderName = caller.name
      ? window.FS.api.folderName(caller.name)
      : null;

    var refFilter = React.useState('all');
    var filter    = refFilter[0];
    var setFilter = refFilter[1];

    var leaves = leavesAll;
    if (canFilter && filter === 'mine' && callerFolderName) {
      leaves = leavesAll.filter(function (t) {
        return (t.assignees || []).indexOf(callerFolderName) !== -1;
      });
    }

    /* Counts for both filter modes — visible in the chip badge. */
    var allCount  = leavesAll.length;
    var mineCount = callerFolderName
      ? leavesAll.filter(function (t) {
          return (t.assignees || []).indexOf(callerFolderName) !== -1;
        }).length
      : 0;

    /* Aggregate counts per column (top-of-board) — based on the
       FILTERED leaves so the totals reflect what's actually shown. */
    var totals = { not_started: 0, in_progress: 0, blocked: 0, done: 0 };
    leaves.forEach(function (t) {
      totals[statusToColumn(t.status)]++;
    });

    return React.createElement('div', { className: 'fs-prog-kanban' },

      /* Sprint 4.10.8 — Mine/All filter chips (gated to non-workers) */
      canFilter ? React.createElement('div', { className: 'fs-prog-kanban__filter' },
        React.createElement('button', {
          type:      'button',
          className: 'fs-prog-kanban__chip'
                     + (filter === 'all' ? ' fs-prog-kanban__chip--active' : ''),
          onClick:   function () { setFilter('all'); },
          'aria-pressed': filter === 'all',
        },
          'All',
          React.createElement('span', { className: 'fs-prog-kanban__chip-count' },
            allCount),
        ),
        React.createElement('button', {
          type:      'button',
          className: 'fs-prog-kanban__chip'
                     + (filter === 'mine' ? ' fs-prog-kanban__chip--active' : ''),
          onClick:   function () { setFilter('mine'); },
          'aria-pressed': filter === 'mine',
          disabled:  !callerFolderName,
        },
          'Mine',
          React.createElement('span', { className: 'fs-prog-kanban__chip-count' },
            mineCount),
        ),
      ) : null,

      /* Top status counts */
      React.createElement('div', { className: 'fs-prog-kanban__totals' },
        COLUMNS.map(function (col) {
          return React.createElement('div', {
            key:       col.key,
            className: 'fs-prog-kanban__total fs-prog-kanban__total--' + col.tone,
          },
            React.createElement('span', { className: 'fs-prog-kanban__total-value' },
              totals[col.key]),
            React.createElement('span', { className: 'fs-prog-kanban__total-label' },
              col.label),
          );
        }),
      ),

      /* Column headers (sticky on scroll) */
      React.createElement('div', { className: 'fs-prog-kanban__columns-header' },
        React.createElement('div', { className: 'fs-prog-kanban__group-spacer' }),
        COLUMNS.map(function (col) {
          return React.createElement('div', {
            key:       col.key,
            className: 'fs-prog-kanban__column-header'
                       + ' fs-prog-kanban__column-header--' + col.tone,
          }, col.label);
        }),
      ),

      /* Rows by WBS group */
      React.createElement('div', { className: 'fs-prog-kanban__rows' },
        parents.map(function (parent) {
          var children = leaves.filter(function (t) {
            return t.parent_id === parent.task_id;
          });
          var collapsed = collapsedSet.has(parent.task_id);
          var byColumn = { not_started: [], in_progress: [], blocked: [], done: [] };
          children.forEach(function (t) {
            byColumn[statusToColumn(t.status)].push(t);
          });

          return React.createElement('div', {
            key:       parent.task_id,
            className: 'fs-prog-kanban__row',
          },
            /* Group header (spans all 4 columns visually) */
            React.createElement('div', { className: 'fs-prog-kanban__group-header' },
              React.createElement('button', {
                type:      'button',
                className: 'fs-prog-kanban__group-chev'
                            + (collapsed ? '' : ' fs-prog-kanban__group-chev--open'),
                'aria-label': collapsed ? 'Expand' : 'Collapse',
                onClick:   function () { onToggleGroup(parent.task_id); },
              }, '▸'),
              React.createElement('span', { className: 'fs-prog-kanban__group-wbs' },
                parent.wbs),
              React.createElement('span', { className: 'fs-prog-kanban__group-name' },
                parent.name),
              React.createElement('span', { className: 'fs-prog-kanban__group-count' },
                children.length + ' ' + (children.length === 1 ? 'task' : 'tasks')),
            ),

            !collapsed
              ? React.createElement('div', { className: 'fs-prog-kanban__group-body' },
                  React.createElement('div', { className: 'fs-prog-kanban__group-spacer' }),
                  COLUMNS.map(function (col) {
                    var items = byColumn[col.key];
                    return React.createElement('div', {
                      key:       col.key,
                      className: 'fs-prog-kanban__cell'
                                  + (items.length === 0 ? ' fs-prog-kanban__cell--empty' : ''),
                      role:      'list',
                      'aria-label': col.label + ' tasks',
                    },
                      items.map(function (t) {
                        var isCritical = criticalSet.has(t.task_id);
                        var isSelected = selectedId === t.task_id;
                        var first = (t.assignees || [])[0];
                        return React.createElement('button', {
                          key:          t.task_id,
                          type:         'button',
                          role:         'listitem',
                          className:    'fs-prog-kanban-card'
                                          + ' fs-prog-kanban-card--' + col.key
                                          + (isCritical ? ' fs-prog-kanban-card--critical' : '')
                                          + (isSelected ? ' fs-prog-kanban-card--selected' : ''),
                          onClick:      function () { onSelect(t); },
                          title:        t.name + ' (' + t.start + ' → ' + t.end + ')',
                          'aria-label': t.name
                                          + (isCritical ? ', critical path' : '')
                                          + ', ' + t.start + ' to ' + t.end,
                        },
                          /* Top row: WBS + critical badge */
                          React.createElement('div', { className: 'fs-prog-kanban-card__top' },
                            React.createElement('span', { className: 'fs-prog-kanban-card__wbs' },
                              t.wbs),
                            isCritical
                              ? React.createElement('span', {
                                  className: 'fs-prog-kanban-card__critical',
                                  title: 'On critical path',
                                }, 'Critical')
                              : null,
                          ),

                          /* Name */
                          React.createElement('div', { className: 'fs-prog-kanban-card__name' },
                            t.name),

                          /* Progress (only when in progress with > 0%) */
                          col.key === 'in_progress' && t.progress_pct > 0
                            ? React.createElement('div', { className: 'fs-prog-kanban-card__progress' },
                                React.createElement('div', {
                                  className: 'fs-prog-kanban-card__progress-bar',
                                  style:     { width: t.progress_pct + '%' },
                                }),
                                React.createElement('span', { className: 'fs-prog-kanban-card__progress-text' },
                                  t.progress_pct + '%'),
                              )
                            : null,

                          /* Footer: assignee + dates */
                          React.createElement('div', { className: 'fs-prog-kanban-card__footer' },
                            first
                              ? React.createElement('div', { className: 'fs-prog-kanban-card__assignee' },
                                  React.createElement(Avatar, { name: shortName(first), size: 'xs' }),
                                  React.createElement('span', null, shortName(first).split(' ')[0]
                                    + ((t.assignees || []).length > 1
                                        ? ' +' + ((t.assignees || []).length - 1) : '')),
                                )
                              : null,
                            React.createElement('div', { className: 'fs-prog-kanban-card__dates' },
                              fmtRange(t.start, t.end)),
                          ),
                        );
                      }),
                    );
                  }),
                )
              : null,
          );
        }),
      ),
    );
  }

  if (!window.FieldSight) window.FieldSight = {};
  window.FieldSight.ProgrammeKanbanBoard = ProgrammeKanbanBoard;
})();
