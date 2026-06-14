/* ==========================================================================
   FieldSight · Toast — Sprint 8.1.4
   --------------------------------------------------------------------------
   Lightweight global toast notification stack. Vanilla JS (no React) so it
   can be called from anywhere — API error handlers, write-flow callbacks,
   import results — without needing to be inside a React component tree.

   API:
     FS.toast.show({ message, tone, duration })
       message  string   — notification text
       tone     string   — 'success' | 'error' | 'warning' | 'info'  (default 'info')
       duration number   — ms before auto-dismiss (default 4000); 0 = no auto-dismiss

   CSS: .fs-toast block in styles/composites.css

   Behaviour:
     • Fixed-position stack, bottom-right, max 4 visible items.
     • Auto-dismiss after `duration` ms; pause timer on hover.
     • Reduced motion: no slide-in animation (still dismisses after duration).
     • Oldest item is dismissed when queue overflows 4.
   ========================================================================== */

(function () {
  'use strict';

  var MAX_TOASTS = 4;
  var DEFAULT_DURATION = 4000;

  var container = null;
  var toasts = [];  /* [{ id, el, timerId }] */
  var nextId = 1;

  function ensureContainer() {
    if (container && document.body.contains(container)) return container;
    container = document.createElement('div');
    container.className = 'fs-toast-stack';
    container.setAttribute('aria-live', 'polite');
    container.setAttribute('aria-atomic', 'false');
    document.body.appendChild(container);
    return container;
  }

  function dismiss(id) {
    var idx = toasts.findIndex(function (t) { return t.id === id; });
    if (idx === -1) return;
    var item = toasts[idx];
    clearTimeout(item.timerId);
    if (item.el && item.el.parentNode) {
      item.el.classList.add('fs-toast--dismissing');
      /* Remove after the transition completes (200 ms). */
      setTimeout(function () {
        if (item.el && item.el.parentNode) item.el.parentNode.removeChild(item.el);
      }, 220);
    }
    toasts.splice(idx, 1);
  }

  function show(opts) {
    opts = opts || {};
    var message  = opts.message  || '';
    var tone     = opts.tone     || 'info';
    var duration = opts.duration != null ? opts.duration : DEFAULT_DURATION;

    var c = ensureContainer();

    /* Overflow — discard the oldest. */
    if (toasts.length >= MAX_TOASTS) dismiss(toasts[0].id);

    var id = nextId++;

    var el = document.createElement('div');
    el.className = 'fs-toast fs-toast--' + tone;
    el.setAttribute('role', 'status');

    var icon = document.createElement('span');
    icon.className = 'fs-toast__icon';
    icon.setAttribute('aria-hidden', 'true');
    var icons = { success: '✓', error: '✕', warning: '⚠', info: 'ℹ' };
    icon.textContent = icons[tone] || icons.info;

    var text = document.createElement('span');
    text.className = 'fs-toast__message';
    text.textContent = message;

    var closeBtn = document.createElement('button');
    closeBtn.type = 'button';
    closeBtn.className = 'fs-toast__close';
    closeBtn.setAttribute('aria-label', 'Dismiss');
    closeBtn.textContent = '×';
    closeBtn.addEventListener('click', function () { dismiss(id); });

    el.appendChild(icon);
    el.appendChild(text);
    el.appendChild(closeBtn);
    c.appendChild(el);

    var item = { id: id, el: el, timerId: null };
    toasts.push(item);

    /* Auto-dismiss timer; pause on hover. */
    if (duration > 0) {
      var remaining = duration;
      var startAt   = Date.now();

      function startTimer() {
        item.timerId = setTimeout(function () { dismiss(id); }, remaining);
      }

      function pauseTimer() {
        clearTimeout(item.timerId);
        remaining -= (Date.now() - startAt);
        if (remaining < 0) remaining = 0;
      }

      function resumeTimer() {
        startAt = Date.now();
        startTimer();
      }

      el.addEventListener('mouseenter', pauseTimer);
      el.addEventListener('mouseleave', resumeTimer);
      startTimer();
    }

    return id;
  }

  if (!window.FS) window.FS = {};
  window.FS.toast = { show: show, dismiss: dismiss };

})();
