/* ==========================================================================
   FieldSight DragDivider — pointer-events based resize handle
   --------------------------------------------------------------------------
   - Pointer Events (uniform mouse / touch / pen)
   - setPointerCapture so drag survives leaving the handle's bounding box
   - Keyboard accessible (←/→ = ±step, Shift = ±largeStep, Home/End = min/max)
   - ARIA role="separator" with aria-valuenow/min/max
   - Adds body class `fs-dragging` so the rest of the app can disable
     pointer-events / user-select while dragging
   - Optional localStorage persistence via `storageKey`

   Usage (controlled):
     <DragDivider
        value={width}
        onChange={setWidth}
        min={280}
        max={480}
        storageKey="fs.appshell.middleWidth"
        ariaLabel="Resize middle column"
     />

   Place as a child of a `position: relative` container — the existing
   `.resize-handle` CSS rule positions it absolute on the right edge.

   Exported to window.FieldSight.DragDivider
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  const DEFAULT_STEP       = 8;
  const DEFAULT_LARGE_STEP = 32;

  function clamp(n, min, max) {
    return Math.max(min, Math.min(max, n));
  }

  function read(key, fallback) {
    if (!key) return fallback;
    try {
      const v = Number(localStorage.getItem(key));
      return Number.isFinite(v) && v > 0 ? v : fallback;
    } catch (e) { return fallback; }
  }

  function write(key, value) {
    if (!key) return;
    try { localStorage.setItem(key, String(value)); } catch (e) { /* quota / private */ }
  }

  function DragDivider({
    value,
    onChange,
    min = 240,
    max = 600,
    step = DEFAULT_STEP,
    largeStep = DEFAULT_LARGE_STEP,
    direction = 'right',
    orientation = 'vertical',
    ariaLabel = 'Resize panel',
    storageKey,
    className,
    style,
  }) {
    const [dragging, setDragging] = React.useState(false);

    const dragRef = React.useRef({ startCoord: 0, startValue: 0, pointerId: null });
    const onChangeRef = React.useRef(onChange);
    onChangeRef.current = onChange;

    const isVertical = orientation === 'vertical';
    const sign = direction === 'right' ? 1 : -1;

    const commit = React.useCallback(function(next) {
      const n = clamp(Math.round(next), min, max);
      onChangeRef.current && onChangeRef.current(n);
      if (storageKey) write(storageKey, n);
    }, [min, max, storageKey]);

    const onPointerDown = React.useCallback(function(e) {
      if (e.button !== 0 && e.pointerType === 'mouse') return;
      e.preventDefault();
      e.currentTarget.setPointerCapture(e.pointerId);
      dragRef.current = {
        startCoord: isVertical ? e.clientX : e.clientY,
        startValue: value,
        pointerId: e.pointerId,
      };
      setDragging(true);
      document.body.classList.add('fs-dragging');
      document.body.style.cursor = isVertical ? 'col-resize' : 'row-resize';
    }, [value, isVertical]);

    const onPointerMove = React.useCallback(function(e) {
      const d = dragRef.current;
      if (d.pointerId == null || e.pointerId !== d.pointerId) return;
      const coord = isVertical ? e.clientX : e.clientY;
      const delta = (coord - d.startCoord) * sign;
      commit(d.startValue + delta);
    }, [commit, isVertical, sign]);

    const endDrag = React.useCallback(function(e) {
      const d = dragRef.current;
      if (d.pointerId == null) return;
      try { e.currentTarget.releasePointerCapture(d.pointerId); } catch (err) {}
      dragRef.current = { startCoord: 0, startValue: 0, pointerId: null };
      setDragging(false);
      document.body.classList.remove('fs-dragging');
      document.body.style.cursor = '';
    }, []);

    const onKeyDown = React.useCallback(function(e) {
      const big = e.shiftKey ? largeStep : step;
      let next = value;
      switch (e.key) {
        case 'ArrowLeft':  if (isVertical)  { next = value - big * sign; } else return; break;
        case 'ArrowRight': if (isVertical)  { next = value + big * sign; } else return; break;
        case 'ArrowUp':    if (!isVertical) { next = value - big * sign; } else return; break;
        case 'ArrowDown':  if (!isVertical) { next = value + big * sign; } else return; break;
        case 'Home':       next = direction === 'right' ? min : max; break;
        case 'End':        next = direction === 'right' ? max : min; break;
        default: return;
      }
      e.preventDefault();
      commit(next);
    }, [value, step, largeStep, isVertical, sign, direction, min, max, commit]);

    const onDoubleClick = React.useCallback(function() {
      commit((min + max) / 2);
    }, [min, max, commit]);

    return React.createElement('div', {
      className: ['resize-handle', dragging ? 'dragging' : null, className].filter(Boolean).join(' '),
      role: 'separator',
      'aria-orientation': isVertical ? 'vertical' : 'horizontal',
      'aria-valuenow': value,
      'aria-valuemin': min,
      'aria-valuemax': max,
      'aria-label': ariaLabel,
      tabIndex: 0,
      onPointerDown: onPointerDown,
      onPointerMove: onPointerMove,
      onPointerUp: endDrag,
      onPointerCancel: endDrag,
      onLostPointerCapture: endDrag,
      onKeyDown: onKeyDown,
      onDoubleClick: onDoubleClick,
      style: Object.assign({ touchAction: 'none' }, style),
    });
  }

  DragDivider.read  = read;
  DragDivider.write = write;
  DragDivider.clamp = clamp;

  if (!window.FieldSight) window.FieldSight = {};
  window.FieldSight.DragDivider = DragDivider;
})();
