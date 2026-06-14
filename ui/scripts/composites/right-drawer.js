/* ==========================================================================
   FieldSight RightDrawer — Layer 5 composite (Sprint 4.7)
   --------------------------------------------------------------------------
   Slide-in detail panel used by full-width pages (currently only
   `/programme`). Renders the page's registered `Right` component
   inside a fixed-position panel that animates in from the right
   edge of the viewport.

   Architecture:
     • Always mounted while the page is full-width — `open` toggles
       the slide animation. Mounting/unmounting on each open would
       break the transition and force re-fetching state held in
       Right components.
     • Backdrop sits behind the panel, semi-opaque. Click → close.
     • ESC key → close (only while open).
     • Close button (×) inside the panel header stays the page-level
       responsibility — pages already render an IconButton when their
       Right detail receives an `onClose` prop, and we pass it through.

   Props:
     open          boolean
     route         string  — current route path (used to resolve
                              the page's Right component)
     selectedItem  any     — pass-through to Right
     onClose       () => void

   Exported to:
     window.FieldSight.RightDrawer
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  function RightDrawer(props) {
    var open     = !!props.open;
    var route    = props.route;
    var sel      = props.selectedItem;
    var onClose  = props.onClose || function () {};

    var drawerRef   = React.useRef(null);
    var triggerRef  = React.useRef(null);

    /* Sprint 8.5.2 — focus management: move focus into drawer on open,
       return focus to trigger on close. */
    React.useEffect(function () {
      if (open) {
        triggerRef.current = document.activeElement;
        var drawer = drawerRef.current;
        if (drawer) {
          var focusable = drawer.querySelector(
            'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
          );
          if (focusable) focusable.focus();
          else drawer.focus();
        }
      } else {
        var trigger = triggerRef.current;
        if (trigger && typeof trigger.focus === 'function') trigger.focus();
      }
    }, [open]);

    /* Sprint 8.5.2 — keyboard: ESC closes, Tab trapped inside drawer while open. */
    React.useEffect(function () {
      if (!open) return undefined;
      function onKey(e) {
        if (e.key === 'Escape') {
          e.stopPropagation();
          onClose();
          return;
        }
        if (e.key !== 'Tab') return;
        var drawer = drawerRef.current;
        if (!drawer) return;
        var focusables = Array.prototype.slice.call(drawer.querySelectorAll(
          'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), ' +
          'textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
        ));
        if (!focusables.length) return;
        var first = focusables[0];
        var last  = focusables[focusables.length - 1];
        if (e.shiftKey) {
          if (document.activeElement === first) { e.preventDefault(); last.focus(); }
        } else {
          if (document.activeElement === last) { e.preventDefault(); first.focus(); }
        }
      }
      window.addEventListener('keydown', onKey);
      return function () { window.removeEventListener('keydown', onKey); };
    }, [open, onClose]);

    /* Resolve the page's Right component fresh on each render — page
       registry is dynamic and cheap to look up. */
    var page  = window.FieldSight.getPageForRoute && window.FieldSight.getPageForRoute(route);
    var Right = page && page.Right;

    return React.createElement(React.Fragment, null,
      React.createElement('div', {
        className: 'fs-right-drawer__backdrop' + (open ? ' fs-right-drawer__backdrop--open' : ''),
        onClick:   onClose,
        'aria-hidden': true,
      }),
      React.createElement('aside', {
        ref:          drawerRef,
        className:    'fs-right-drawer' + (open ? ' fs-right-drawer--open' : ''),
        role:         'dialog',
        'aria-modal': true,
        'aria-label': 'Detail panel',
        'aria-hidden': !open,
        tabIndex:     -1,
      },
        Right
          ? React.createElement(Right, {
              selectedItem: sel,
              onClose:      onClose,
            })
          : null,
      ),
    );
  }

  if (!window.FieldSight) window.FieldSight = {};
  window.FieldSight.RightDrawer = RightDrawer;
})();
