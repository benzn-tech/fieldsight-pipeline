/* ==========================================================================
   FieldSight TaskCard — Layer 5 composite
   --------------------------------------------------------------------------
   Renders a task as a clickable Card row: avatar + 2-line title +
   status badge + due time. The `isMine` modifier strengthens the
   left edge with an accent border so a user's own tasks stand out
   when mixed with the team's.

   Props:
     task       { id, title, assignee, status, statusTone, dueTime, ... }
     isMine     boolean — apply --mine accent border
     onSelect   (task) => void — click handler

   Exported to:
     window.FieldSight.TaskCard
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  function TaskCard(props) {
    var Card   = window.FieldSight.Card;
    var Avatar = window.FieldSight.Avatar;
    var Badge  = window.FieldSight.Badge;

    var task     = props.task;
    var isMine   = !!props.isMine;
    var onSelect = props.onSelect;

    var className = 'fs-task-card' + (isMine ? ' fs-task-card--mine' : '');

    return React.createElement(Card, {
      padding: 'sm',
      onClick: onSelect ? function() { onSelect(task); } : undefined,
      className: className,
    },
      React.createElement(Card.Body, null,
        React.createElement('div', { className: 'fs-task-card__row' },
          React.createElement(Avatar, { name: task.assignee, size: 'sm' }),
          React.createElement('div', { className: 'fs-task-card__main' },
            React.createElement('div', { className: 'fs-task-card__title' },
              task.title),
          ),
          React.createElement('div', { className: 'fs-task-card__meta' },
            React.createElement(Badge, { tone: task.statusTone, size: 'sm' },
              task.status),
            React.createElement('span', { className: 'fs-task-card__due' },
              task.dueTime),
          ),
        ),
      ),
    );
  }

  if (!window.FieldSight) window.FieldSight = {};
  window.FieldSight.TaskCard = TaskCard;
})();
