/* ==========================================================================
   FieldSight TaskCard — Layer 5 composite
   --------------------------------------------------------------------------
   Renders a task as a clickable Card row: avatar (or check button) +
   2-line title + status badge + due time. The `isMine` modifier
   strengthens the left edge with an accent border so a user's own
   tasks stand out when mixed with the team's.

   Sprint 2.4 (PLAN Phase D) adds optional check-off:
     • when `checkable` is true and the task carries topic_id +
       actionIndex, the avatar slot becomes a circular checkbox
     • clicking it fires FS.api.actions.toggleAction (optimistic) and
       transitions the row through:
            border pulse → line-through → fade-out
       The animation respects prefers-reduced-motion via the global
       media query in tokens.css (which neutralises animation duration).
     • onAnimationEnd → onCheckedOff(task) so the parent can drop the
       row from its rendered list.

   Props:
     task           { id, title, assignee, status, statusTone, dueTime,
                      topic_id, actionIndex, ... }
     isMine         boolean — apply --mine accent border
     onSelect       (task) => void — click handler on the row body
     checkable      boolean — show check button instead of avatar
     date           'YYYY-MM-DD' — passed to FS.api.actions.toggleAction
     onCheckedOff   (task) => void — called once the fade-out finishes

   Exported to:
     window.FieldSight.TaskCard
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  function TaskCard(props) {
    var Card    = window.FieldSight.Card;
    var Avatar  = window.FieldSight.Avatar;
    var Badge   = window.FieldSight.Badge;
    var NavIcon = window.FieldSight.NavIcon;

    var task     = props.task;
    var isMine   = !!props.isMine;
    var onSelect = props.onSelect;
    var checkable = !!props.checkable && task && task.topic_id != null && task.actionIndex != null;

    /* checkingOff: true → row enters animation. Stays true until
       onAnimationEnd; the parent's onCheckedOff then unmounts us. */
    var refCO = React.useState(false);
    var checkingOff    = refCO[0];
    var setCheckingOff = refCO[1];

    function startCheckOff(e) {
      if (e) { e.stopPropagation(); e.preventDefault(); }
      if (checkingOff) return;
      setCheckingOff(true);

      /* Sprint 8.5.4 — announce to the global polite live region. */
      var region = document.getElementById('fs-live-region');
      if (region) region.textContent = 'Marked complete';

      var api = window.FS && window.FS.api && window.FS.api.actions;
      if (!api) return;

      api.toggleAction({
        date:         props.date,
        topic_id:     task.topic_id,
        action_index: task.actionIndex,
        checked:      true,
        action_text:  task.title,
      }).catch(function (err) {
        /* If the persist call fails, abort the animation and let the
           user retry. */
        console.error('[TaskCard] toggleAction failed', err);
        setCheckingOff(false);
      });
    }

    function onAnimationEnd() {
      if (checkingOff && props.onCheckedOff) props.onCheckedOff(task);
    }

    var className = 'fs-task-card'
      + (isMine ? ' fs-task-card--mine' : '')
      + (props.selected ? ' fs-card--selected' : '')
      + (checkingOff ? ' fs-task-card--checking-off' : '');

    /* Leading slot: avatar by default, circular check button when
       checkable. The button sits on top of the row click handler
       — internal stopPropagation prevents the row's onSelect from
       firing on check. */
    var leading = checkable
      ? React.createElement('button', {
          type:        'button',
          className:   'fs-task-card__check',
          onClick:     startCheckOff,
          'aria-label': 'Mark task complete',
          'aria-pressed': checkingOff,
          disabled:    checkingOff,
        },
          NavIcon ? React.createElement(NavIcon, {
            name: 'check', size: 14,
          }) : '✓',
        )
      : React.createElement(Avatar, { name: task.assignee, size: 'sm' });

    return React.createElement(Card, {
      padding:   'sm',
      onClick:   onSelect && !checkingOff ? function () { onSelect(task); } : undefined,
      className: className,
    },
      React.createElement(Card.Body, {
        onAnimationEnd: onAnimationEnd,
      },
        React.createElement('div', { className: 'fs-task-card__row' },
          leading,
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
