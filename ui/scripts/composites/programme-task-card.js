/* ==========================================================================
   FieldSight ProgrammeTaskCard — Layer 5 composite (Sprint 4.10)
   --------------------------------------------------------------------------
   Today-page variant of TaskCard for rows sourced from the Programme
   instead of yesterday's daily-report action items. Differs from
   TaskCard on three axes:

     • No checkbox — programme progress is multi-day, not a single
       binary toggle. Closing a programme task is a Sprint 5 problem
       (cascade, status flow, etc.).
     • WBS code prefix — mono "1.3" / "2.1" so the user reads the
       task's place in the work breakdown structure at a glance.
     • Progress bar — dominant footer element, since the work-on-this
       has accumulated and the user wants to see "how much further".

   Click → page navigates to /programme?task=T-XXX&from=today, and
   ProgrammeMiddleColumn deep-links into the drawer (Sprint 4.10.1).

   Props:
     row          { source:'programme', task_id, wbs, name, start, end,
                    duration_days, progress_pct, status, critical,
                    day_index, day_total, assignees, ... }
     onSelect     (row) => void

   Exported to:
     window.FieldSight.ProgrammeTaskCard
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  function ProgrammeTaskCard(props) {
    var fs    = window.FieldSight;
    var Card  = fs.Card;
    var Badge = fs.Badge;

    var row      = props.row || {};
    var onSelect = props.onSelect;

    var statusLabel = ({
      not_started: 'Not started',
      in_progress: 'In progress',
      completed:   'Done',
      blocked:     'Blocked',
      delayed:     'Delayed',
    })[row.status] || row.status;

    var statusTone = ({
      not_started: 'neutral',
      in_progress: 'info',
      completed:   'success',
      blocked:     'danger',
      delayed:     'warning',
    })[row.status] || 'neutral';

    var progress = Math.max(0, Math.min(100, row.progress_pct || 0));

    return React.createElement(Card, {
      padding:   'sm',
      onClick:   onSelect ? function () { onSelect(row); } : undefined,
      className: 'fs-programme-task-card'
                 + (row.critical ? ' fs-programme-task-card--critical' : ''),
    },
      React.createElement(Card.Body, null,

        /* Top row — WBS + name + status + critical */
        React.createElement('div', { className: 'fs-programme-task-card__head' },
          React.createElement('span', { className: 'fs-programme-task-card__wbs' },
            row.wbs || '—'),
          React.createElement('span', { className: 'fs-programme-task-card__name' },
            row.name || '—'),
          React.createElement('div', { className: 'fs-programme-task-card__badges' },
            row.critical
              ? React.createElement(Badge, {
                  tone: 'danger', size: 'sm', variant: 'subtle',
                }, 'Critical')
              : null,
            React.createElement(Badge, {
              tone: statusTone, size: 'sm', prefixDot: true,
            }, statusLabel),
          ),
        ),

        /* Day N of M */
        row.day_index && row.day_total
          ? React.createElement('div', { className: 'fs-programme-task-card__day' },
              'Day ' + row.day_index + ' of ' + row.day_total)
          : null,

        /* Progress bar */
        React.createElement('div', { className: 'fs-programme-task-card__progress' },
          React.createElement('div', { className: 'fs-programme-task-card__progress-track' },
            React.createElement('div', {
              className: 'fs-programme-task-card__progress-fill',
              style:     { width: progress + '%' },
            }),
          ),
          React.createElement('span', { className: 'fs-programme-task-card__progress-text' },
            progress + '%'),
        ),
      ),
    );
  }

  if (!window.FieldSight) window.FieldSight = {};
  window.FieldSight.ProgrammeTaskCard = ProgrammeTaskCard;
})();
