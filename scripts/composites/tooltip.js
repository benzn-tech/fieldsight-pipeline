/* ==========================================================================
   FieldSight Tooltip — Sprint 8.11.3
   --------------------------------------------------------------------------
   Lightweight CSS-positioned tooltip wrapping any element. Shows on
   hover + keyboard focus, dismisses on mouseout + blur. No external
   library, no portal — relies on `position: absolute` against the wrapper.

   Props:
     content    string                — tooltip body text
     placement  'top'|'bottom'|'right'|'left'   (default 'top')
     delay      ms before show         (default 600)
     children   single React node      — the wrapped trigger element
     wrapEl     'span' | 'div'         (default 'span') — wrapper tag

   The trigger receives no DOM changes; the tooltip is rendered as a
   sibling absolute-positioned `<span>`. Reduced motion respected via
   the .fs-tooltip CSS rule (no transition under prefers-reduced-motion).

   Exported to: window.FieldSight.Tooltip
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  function Tooltip(props) {
    var content   = props.content;
    var placement = props.placement || 'top';
    var delay     = props.delay != null ? props.delay : 600;
    var Wrap      = props.wrapEl || 'span';

    var refOpen = React.useState(false);
    var open    = refOpen[0];
    var setOpen = refOpen[1];
    var timerRef = React.useRef(null);

    function clearTimer() {
      if (timerRef.current) {
        clearTimeout(timerRef.current);
        timerRef.current = null;
      }
    }

    function scheduleShow() {
      clearTimer();
      timerRef.current = setTimeout(function () { setOpen(true); }, delay);
    }
    function hide() {
      clearTimer();
      setOpen(false);
    }

    React.useEffect(function () { return clearTimer; }, []);

    if (!content) return props.children || null;

    return React.createElement(Wrap, {
      className:   'fs-tooltip-wrap',
      onMouseEnter: scheduleShow,
      onMouseLeave: hide,
      onFocus:      scheduleShow,
      onBlur:       hide,
    },
      props.children,
      open ? React.createElement('span', {
        className: 'fs-tooltip fs-tooltip--' + placement,
        role:      'tooltip',
      }, content) : null,
    );
  }

  if (!window.FieldSight) window.FieldSight = {};
  window.FieldSight.Tooltip = Tooltip;
})();
