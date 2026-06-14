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

   Sprint 8.3.1 — optional float pill (total slack) shown when showFloat=true.
   Sprint 8.3.3 — optional baseline ghost bar shown when showBaseline=true
                  and baselineStart / baselineEnd are provided.

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
     showFloat       boolean — render the float pill (Sprint 8.3.1)
     floatDays       number  — total slack in days (0 = critical)
     showBaseline    boolean — render baseline ghost bar (Sprint 8.3.3)
     baselineStart   ISO date | null
     baselineEnd     ISO date | null

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

  /* Float pill: 0d = critical/danger, 1–3d = amber warning, >3d = success */
  function FloatPill(props) {
    var days = props.days;
    if (days == null) return null;
    var tone = days === 0  ? 'critical'
             : days <= 3  ? 'warn'
             :               'ok';
    return React.createElement('span', {
      className: 'fs-gantt-float-pill fs-gantt-float-pill--' + tone,
      title:     days === 0
                   ? 'Critical path — zero float'
                   : days + ' day' + (days === 1 ? '' : 's') + ' float',
    }, days + 'd');
  }

  function GanttRow(props) {
    var t             = props.task;
    var origin        = props.programmeStart;
    var ppd           = props.pixelsPerDay || 24;
    var critical      = !!props.critical;
    var selected      = !!props.selected;
    var dragPreview   = props.dragPreview;
    var onDragStart   = props.onDragStart;
    var onDragMove    = props.onDragMove;
    var onDragEnd     = props.onDragEnd;
    var showFloat     = !!props.showFloat;
    var floatDays     = props.floatDays;
    var showBaseline  = !!props.showBaseline;
    var baselineStart = props.baselineStart || null;
    var baselineEnd   = props.baselineEnd   || null;

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

    /* Baseline ghost bar geometry (Sprint 8.3.3) */
    var baselineBar = null;
    if (showBaseline && !isGroup && baselineStart && baselineEnd) {
      var bOffset = Math.max(0, diffDays(origin, baselineStart)) * ppd;
      var bWidth  = Math.max(ppd * 0.5, (diffDays(baselineStart, baselineEnd) + 1) * ppd);
      baselineBar = React.createElement('div', {
        className: 'fs-gantt-bar--baseline',
        style:     { left: bOffset + 'px', width: bWidth + 'px' },
        title:     'Planned: ' + baselineStart + ' → ' + baselineEnd,
      });
    }

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

    /* Sprint 8.5.5 — keyboard operability for Gantt bar.
       ArrowLeft/Right: shift whole task by 1 day (move mode).
       Shift+ArrowLeft/Right: extend/shrink end date by 1 day.
       Enter/Space: select the task (opens editor in parent). */
    function onBarKeyDown(e) {
      if (!draggable) {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          if (props.onSelect) props.onSelect(t);
        }
        return;
      }
      var handled = false;
      if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
        e.preventDefault();
        handled = true;
        var delta = e.key === 'ArrowLeft' ? -1 : 1;
        var api   = window.FS && window.FS.api;
        if (!api) return;
        if (e.shiftKey) {
          /* Extend/shrink end date */
          var newEnd = api.addDaysISO(effEnd, delta);
          if (newEnd >= effStart && onDragStart && onDragEnd) {
            onDragStart(t, 'resize-end', 0);
            /* Commit directly via updateTask if available on context */
            if (props.onKeyboardMove) {
              props.onKeyboardMove({ task_id: t.task_id, start: effStart, end: newEnd });
            }
            onDragEnd();
          }
        } else {
          /* Move whole task */
          var ns = api.addDaysISO(effStart, delta);
          var ne = api.addDaysISO(effEnd,   delta);
          if (props.onKeyboardMove) {
            props.onKeyboardMove({ task_id: t.task_id, start: ns, end: ne });
          }
        }
      }
      if (!handled && (e.key === 'Enter' || e.key === ' ')) {
        e.preventDefault();
        if (props.onSelect) props.onSelect(t);
      }
    }

    /* Sprint 8.5.5 — aria-value bounds: days from programme start */
    var ariaMin   = 0;
    var ariaMax   = props.programmeDurationDays || 365;
    var ariaStart = Math.max(0, diffDays(origin, effStart));

    return React.createElement('div', {
      className: 'fs-gantt-row',
    },
      /* Baseline ghost bar behind the current bar (Sprint 8.3.3) */
      baselineBar,

      /* Current bar */
      React.createElement('div', {
        className:    barClass,
        style:        { left: startOffset + 'px', width: width + 'px' },
        title:        t.name + '  (' + effStart + ' → ' + effEnd + ')'
                       + (draggable ? '  · drag or use arrow keys to reschedule' : ''),
        /* Sprint 8.5.5 — keyboard accessibility */
        tabIndex:     isGroup ? -1 : 0,
        role:         isGroup ? undefined : 'slider',
        'aria-label': t.name + ', ' + effStart + ' to ' + effEnd,
        'aria-valuemin':  ariaMin,
        'aria-valuemax':  ariaMax,
        'aria-valuenow':  ariaStart,
        'aria-valuetext': effStart + ' to ' + effEnd,
        onPointerDown: onPointerDown,
        onClick:       onBarClick,
        onKeyDown:     onBarKeyDown,
      },
        progress > 0 && !isGroup ? React.createElement('div', {
          className: 'fs-gantt-bar__progress',
          style:     { width: progress + '%' },
        }) : null,

        width > 60 ? React.createElement('span', {
          className: 'fs-gantt-bar__label',
        }, t.name) : null,
      ),

      /* Float pill (Sprint 8.3.1) — rendered to the right of the bar */
      showFloat && !isGroup && floatDays != null
        ? React.createElement(FloatPill, {
            days: floatDays,
            key:  'float-' + t.task_id,
          })
        : null,
    );
  }

  if (!window.FieldSight) window.FieldSight = {};
  window.FieldSight.GanttRow = GanttRow;
})();
