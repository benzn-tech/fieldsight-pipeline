/* ==========================================================================
   FieldSight TaskTreeCell — Layer 5 composite (Sprint 4.4)
   --------------------------------------------------------------------------
   Renders the left-side WBS cell for one row in the Gantt:
     [chevron]  WBS code   Task name   [assignees]

   Used by both group rows and leaf rows. Chevron only renders for
   group rows.

   Props:
     task         { task_id, wbs, name, status, assignees }
     isGroup      boolean
     expanded     boolean — only used when isGroup
     indent       number — depth-2 max for this prototype
     onToggle     () => void — chevron click (only when isGroup)
     onSelect     (task) => void — row click
     selected     boolean
     critical     boolean — schedule-driving task highlight

   Exported to:
     window.FieldSight.TaskTreeCell
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  function TaskTreeCell(props) {
    var t        = props.task || {};
    var isGroup  = !!props.isGroup;
    var expanded = !!props.expanded;
    var indent   = props.indent || 0;
    var critical = !!props.critical;

    var className = 'fs-gantt-tree__cell'
      + (isGroup  ? ' fs-gantt-tree__cell--group'   : '')
      + (props.selected ? ' fs-gantt-tree__cell--selected' : '')
      + (critical ? ' fs-gantt-tree__cell--critical' : '');

    return React.createElement('div', {
      className: className,
      style:     { paddingLeft: (8 + indent * 14) + 'px' },
      onClick:   function () { if (props.onSelect) props.onSelect(t); },
      role:      'button',
      tabIndex:  0,
    },
      isGroup
        ? React.createElement('button', {
            type:      'button',
            className: 'fs-gantt-tree__chev'
                       + (expanded ? ' fs-gantt-tree__chev--open' : ''),
            'aria-label': expanded ? 'Collapse' : 'Expand',
            onClick:   function (e) {
              e.stopPropagation();
              if (props.onToggle) props.onToggle();
            },
          }, '▸')
        : React.createElement('span', { className: 'fs-gantt-tree__chev-spacer' }),

      React.createElement('span', { className: 'fs-gantt-tree__wbs' },
        t.wbs || ''),
      React.createElement('span', { className: 'fs-gantt-tree__name' },
        t.name || t.task_id),

      !isGroup && (t.assignees || []).length
        ? React.createElement('span', { className: 'fs-gantt-tree__assignees' },
            (t.assignees[0] || '').replace(/_/g, ' ')
              + (t.assignees.length > 1 ? ' +' + (t.assignees.length - 1) : ''))
        : null,
    );
  }

  if (!window.FieldSight) window.FieldSight = {};
  window.FieldSight.TaskTreeCell = TaskTreeCell;
})();
