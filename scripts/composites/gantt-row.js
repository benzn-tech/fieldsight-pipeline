/* ==========================================================================
   FieldSight GanttRow — Layer 5 composite (Sprint 4.4 + drag in 4.9)
   --------------------------------------------------------------------------
   One timeline-side row: a horizontal bar at (start - programmeStart)
   pixels offset, of width (duration_days × pixelsPerDay). Click selects
   the task.

   Sprint 4.9 — bar is now draggable. Three drag modes:
     • L1 move      — pointerdown on bar body → translate whole bar
     • L2 resize-S  — pointerdown on first 8 px → only `start` moves
     • L2 resize-E  — pointerdown on last  8 px → only `end`   moves

   Drag is disabled for:
     • group rows (computed summary, not editable)
     • completed tasks (shouldn't move history)

   Snapping is implicit: deltaPx / pixelsPerDay → Math.round → days.

   Bar visual:
     • Status colour (completed / in_progress / blocked / delayed /
       not_started / group)
     • Critical-path tasks get a danger-tone stripe
     • Progress fills the bar from the left as a darker overlay
     • Group rows render a thinner summary bar with rolled-up dates

   Props:
     task            { task_id, start, end, duration_days,
                        progress_pct, status, ... }
     programmeStart  ISO date — origin
     pixelsPerDay    number
     critical        boolean
     selected        boolean
     onSelect        (task) => void
     onDragStart     (task, mode, originX) => void
     onDragMove      (clientX) => void
     onDragEnd       () => void
     dragPreview     { start, end } | null  — overrides bar position
                                              while a drag is in flight

   Exported to:
     window.FieldSight.GanttRow
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  var EDGE_HIT = 8; /* px on each end of the bar that grabs the resize handle */

  function diffDays(fromISO, toISO) {
    var a = new Date(fromISO + 'T00:00:00Z').getTime();
    var b = new Date(toISO   + 'T00:00:00Z').getTime();
    return Math.round((b - a) / 86400000);
  }

  function GanttRow(props) {
    var t           = props.task;
    var origin      = props.programmeStart;
    var ppd         = props.pixelsPerDay || 24;
    var critical    = !!props.critical;
    var selected    = !!props.selected;
    var dragPreview = props.dragPreview;
    var onDragStart = props.onDragStart;
    var onDragMove  = props.onDragMove;
    var onDragEnd   = props.onDragEnd;

    if (!t || !t.start || !t.end) {
      return React.createElement('div', { className: 'fs-gantt-row' });
    }

    /* Effective start/end — when a drag is in flight on this row,
       dragPreview takes precedence so the bar follows the pointer
       optimistically. */
    var effStart = dragPreview ? dragPreview.start : t.start;
    var effEnd   = dragPreview ? dragPreview.end   : t.end;

    var startOffset = Math.max(0, diffDays(origin, effStart)) * ppd;
    var widthDays   = diffDays(effStart, effEnd) + 1;
    var width       = Math.max(ppd * 0.5, widthDays * ppd);
    var progress    = Math.max(0, Math.min(100, t.progress_pct || 0));

    var isGroup     = t.status === 'group';
    var isCompleted = t.status === 'completed';
    var draggable   = !isGroup && !isCompleted && onDragStart;

    var barClass = 'fs-gantt-bar'
      + ' fs-gantt-bar--' + (t.status || 'not_started')
      + (critical    ? ' fs-gantt-bar--critical'  : '')
      + (selected    ? ' fs-gantt-bar--selected'  : '')
      + (isGroup     ? ' fs-gantt-bar--group'     : '')
      + (dragPreview ? ' fs-gantt-bar--dragging'  : '')
      + (draggable   ? ' fs-gantt-bar--draggable' : '');

    /* Decide drag mode by where on the bar the pointer landed —
       offsetX from the bar's own left edge. */
    function modeFromOffset(barEl, clientX) {
      var rect = barEl.getBoundingClientRect();
      var offX = clientX - rect.left;
      if (offX <= EDGE_HIT)              return 'resize-start';
      if (offX >= rect.width - EDGE_HIT) return 'resize-end';
      return 'move';
    }

    function onPointerDown(e) {
      if (!draggable) return;
      /* Only respond to the primary button. Right-click + middle
         click should still bubble up (e.g., context menu). */
      if (e.button !== 0) return;
      e.stopPropagation();
      var bar  = e.currentTarget;
      var mode = modeFromOffset(bar, e.clientX);

      /* Capture pointer so move/up still fire even if the cursor
         leaves the bar mid-drag. */
      try { bar.setPointerCapture(e.pointerId); } catch (_) {}

      if (onDragStart) onDragStart(t, mode, e.clientX);

      function onMove(ev)  { if (onDragMove) onDragMove(ev.clientX); }
      function onUp()      {
        bar.removeEventListener('pointermove', onMove);
        bar.removeEventListener('pointerup',   onUp);
        bar.removeEventListener('pointercancel', onUp);
        if (onDragEnd) onDragEnd();
      }
      bar.addEventListener('pointermove', onMove);
      bar.addEventListener('pointerup',   onUp);
      bar.addEventListener('pointercancel', onUp);
    }

    /* Click selects the task — but pointerup that follows a drag
       doesn't generate a click in the same React event cycle, so
       we rely on a heuristic: if no drag was in flight, treat as a
       select. The page's drag controller resets dragPreview to
       null after pointerup; here we use that to gate the click. */
    function onBarClick(e) {
      if (dragPreview) return;
      if (props.onSelect) props.onSelect(t);
    }

    return React.createElement('div', {
      className: 'fs-gantt-row',
    },
      React.createElement('div', {
        className:    barClass,
        style:        { left: startOffset + 'px', width: width + 'px' },
        title:        t.name + '  (' + effStart + ' → ' + effEnd + ')'
                       + (draggable ? '  · drag to reschedule' : ''),
        onPointerDown: onPointerDown,
        onClick:       onBarClick,
      },
        progress > 0 && !isGroup ? React.createElement('div', {
          className: 'fs-gantt-bar__progress',
          style:     { width: progress + '%' },
        }) : null,

        width > 60 ? React.createElement('span', {
          className: 'fs-gantt-bar__label',
        }, t.name) : null,
      ),
    );
  }

  if (!window.FieldSight) window.FieldSight = {};
  window.FieldSight.GanttRow = GanttRow;
})();
