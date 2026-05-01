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

    /* ESC handler — only listen while open so we don't intercept
       Escape on other pages. */
    React.useEffect(function () {
      if (!open) return undefined;
      function onKey(e) {
        if (e.key === 'Escape') {
          e.stopPropagation();
          onClose();
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
        'aria-hidden': !open,
      }),
      React.createElement('aside', {
        className:    'fs-right-drawer' + (open ? ' fs-right-drawer--open' : ''),
        role:         'complementary',
        'aria-label': 'Detail panel',
        'aria-hidden': !open,
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
