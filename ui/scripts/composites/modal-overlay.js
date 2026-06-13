/* ==========================================================================
   FieldSight ModalOverlay — Layer 5 composite (Sprint 5.0 / 5.7.1)
   --------------------------------------------------------------------------
   Centred modal primitive used by Programme task editor (5.1) and import
   modal (5.4). Architecturally a sibling of `RightDrawer`: same backdrop +
   ESC + always-mounted-while-open pattern, but the panel sits centred
   in the viewport instead of pinned to the right edge.

   Why a separate primitive (not a RightDrawer variant): drawers are for
   resident detail panels that benefit from coexisting with the page;
   modals are transient, blocking, focus-stealing surfaces that exit
   through OK/Cancel. Different lifetime, different layering (modals
   above drawers via --z-modal=500 vs drawer's z=50).

   Sprint 5.7.1 — uses `ReactDOM.createPortal` to mount at document.body.
   Without the portal, when the editor is mounted from inside RightDrawer
   (which has `overflow: hidden` + `transform: translateX(...)` creating
   its own stacking context), the modal got clipped to the drawer's
   bounds AND its z-index was scoped relative to the drawer — making
   the centred panel render off-centre and partially hidden. Portaling
   to the body lifts the entire modal subtree out of every parent
   stacking context, restoring true viewport-centred positioning and
   correct z-layer ordering against the drawer.

   Props:
     open              boolean
     onClose           () => void
     title             string?    — rendered in the modal header (h2)
     ariaLabel         string?    — fallback when title isn't set
     size              'sm'|'md'|'lg' (default 'md')
     closeOnBackdrop   boolean (default true) — set false for forms with
                        unsaved input (5.1 editor passes false)
     children          modal body content

   Exported to:
     window.FieldSight.ModalOverlay
   ========================================================================== */

/* global React, ReactDOM, window, document */

(function () {
  'use strict';

  function ModalOverlay(props) {
    var open    = !!props.open;
    var onClose = props.onClose || function () {};
    var title   = props.title || null;
    var size    = props.size || 'md';
    var closeOnBackdrop = props.closeOnBackdrop !== false;
    var panelRef        = React.useRef(null);
    var triggerRef      = React.useRef(null);  /* element that opened the modal */

    /* Sprint 8.5.2 — focus management: move focus into modal on open,
       return focus to trigger on close. */
    React.useEffect(function () {
      if (open) {
        /* Save the element that had focus before the modal opened */
        triggerRef.current = document.activeElement;
        /* Move focus to the first focusable element inside the modal */
        var panel = panelRef.current;
        if (panel) {
          var focusable = panel.querySelector(
            'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
          );
          if (focusable) focusable.focus();
          else panel.focus();
        }
      } else {
        /* Return focus to the element that opened the modal */
        var trigger = triggerRef.current;
        if (trigger && typeof trigger.focus === 'function') trigger.focus();
      }
    }, [open]);

    /* Sprint 8.5.2 — keyboard: ESC closes, Tab/Shift+Tab trapped inside modal.
       Also hide background content from AT while modal is open. */
    React.useEffect(function () {
      if (!open) return undefined;
      function onKey(e) {
        if (e.key === 'Escape') {
          e.stopPropagation();
          onClose();
          return;
        }
        if (e.key !== 'Tab') return;
        var panel = panelRef.current;
        if (!panel) return;
        var focusables = Array.prototype.slice.call(panel.querySelectorAll(
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

      /* Hide background content from assistive technology */
      var appRoot = document.getElementById('root');
      if (appRoot) appRoot.setAttribute('aria-hidden', 'true');

      return function () {
        window.removeEventListener('keydown', onKey);
        var r = document.getElementById('root');
        if (r) r.removeAttribute('aria-hidden');
      };
    }, [open, onClose]);

    var titleId = React.useMemo(function () {
      return 'fs-modal-title-' + Math.random().toString(36).slice(2, 8);
    }, []);

    function onBackdropClick() {
      if (closeOnBackdrop) onClose();
    }

    var tree = React.createElement(React.Fragment, null,
      React.createElement('div', {
        className:     'fs-modal__backdrop' + (open ? ' fs-modal__backdrop--open' : ''),
        onClick:       onBackdropClick,
        'aria-hidden': !open,
      }),
      React.createElement('div', {
        ref:                 panelRef,
        className:           'fs-modal' + (open ? ' fs-modal--open' : '') + ' fs-modal--' + size,
        role:                'dialog',
        'aria-modal':        true,
        'aria-labelledby':   title ? titleId : undefined,
        'aria-label':        !title ? (props.ariaLabel || 'Dialog') : undefined,
        'aria-hidden':       !open,
        tabIndex:            -1,
        onClick: function (e) { e.stopPropagation(); },
      },
        title
          ? React.createElement('header', { className: 'fs-modal__header' },
              React.createElement('h2', { id: titleId, className: 'fs-modal__title' }, title),
              React.createElement('button', {
                type:        'button',
                className:   'fs-modal__close',
                'aria-label': 'Close',
                onClick:     onClose,
              }, '×'),
            )
          : null,
        React.createElement('div', { className: 'fs-modal__body' }, props.children),
      ),
    );

    /* Portal lift — see Sprint 5.7.1 note in the file header. Falls back
       to in-tree rendering only when ReactDOM/document.body aren't
       available (e.g. node smoke tests where document is stubbed). */
    if (typeof ReactDOM !== 'undefined' && ReactDOM.createPortal
        && typeof document !== 'undefined' && document.body) {
      return ReactDOM.createPortal(tree, document.body);
    }
    return tree;
  }

  if (!window.FieldSight) window.FieldSight = {};
  window.FieldSight.ModalOverlay = ModalOverlay;
})();
