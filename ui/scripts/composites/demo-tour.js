/* ==========================================================================
   FieldSight DemoTour — Sprint 8.9.2
   --------------------------------------------------------------------------
   Lightweight guided-tour overlay activated by `?demo=1` in the URL.
   Renders a non-blocking tooltip card pinned bottom-centre with Prev /
   Next / Done controls and a step counter; each step navigates to the
   relevant route and highlights a target element via box-shadow ring.

   Constraints:
     • No overlay backdrop — the user can still interact with the page.
     • Highlight is a pulse ring on `.fs-demo-highlight`; CSS lives in
       composites.css and is reduced-motion-safe.
     • Tour state lives in component (not persisted) — closing exits
       cleanly; reopening starts at step 1.

   API:
     window.FieldSight.DemoTour                 — React component
     window.FieldSight.shouldRunDemoTour()      — true when ?demo=1

   Mounted by AppShell when the URL flag is set.
   ========================================================================== */

/* global React, window, document */

(function () {
  'use strict';

  var STEPS = [
    {
      route:     '/today',
      highlight: '.fs-kpi-strip',
      title:     'Your day at a glance',
      text:      'KPI strip surfaces the numbers that matter for today — open actions, urgent items, and on-site headcount.',
    },
    {
      route:     '/today',
      highlight: '.fs-urgent-card',
      title:     'Urgent items surface automatically',
      text:      'High-risk safety flags and overdue actions are pulled to the top so nothing slips through.',
    },
    {
      route:     '/timeline?date=2026-04-29&user=Jarley_Trainor',
      highlight: '.fs-topic-card',
      title:     'Timeline of every site conversation',
      text:      'Tap any topic to expand the full transcript, photos, and linked actions.',
    },
    {
      route:     '/programme',
      highlight: '.fs-gantt-row',
      title:     'Drag tasks to reschedule',
      text:      'The Gantt cascades dependencies automatically — slack, float, and over-allocation update in real time.',
    },
    {
      route:     '/safety',
      highlight: '.fs-safety-flag-row',
      title:     'Safety flags with risk levels',
      text:      'Every observation is tagged risk-high / medium / low, and high-risk items are surfaced on Today.',
    },
    /* Sprint 9 — /insights showcase. */
    {
      route:     '/insights',
      highlight: '.fs-bar-stack',
      title:     'Spot patterns, not single incidents',
      text:      'Insights rolls last-7-day safety + quality issues by subcontractor and tag — PMs spot trends without reading every report.',
    },
    /* Sprint 10 B.6 — /library showcase. Every role has template:manage:self so
       the tab is visible regardless of which role the demo uses. */
    {
      route:     '/library',
      highlight: '.fs-library__list',
      title:     'Your company\'s report templates',
      text:      'Upload your own report template — AI extracts the structure so all generated reports match your firm\'s format and branding.',
    },
  ];

  var HIGHLIGHT_CLASS = 'fs-demo-highlight';

  function clearAllHighlights() {
    var prev = document.querySelectorAll('.' + HIGHLIGHT_CLASS);
    for (var i = 0; i < prev.length; i++) {
      prev[i].classList.remove(HIGHLIGHT_CLASS);
    }
  }

  function applyHighlight(selector) {
    clearAllHighlights();
    if (!selector) return;
    /* Wait for the page to render after navigation. */
    setTimeout(function () {
      var el = document.querySelector(selector);
      if (el) el.classList.add(HIGHLIGHT_CLASS);
    }, 250);
  }

  function shouldRunDemoTour() {
    try {
      var p = new URLSearchParams(window.location.search);
      return p.get('demo') === '1';
    } catch (_) { return false; }
  }

  function DemoTour(props) {
    var refIdx = React.useState(0);
    var step    = refIdx[0];
    var setStep = refIdx[1];

    var refOpen = React.useState(true);
    var open    = refOpen[0];
    var setOpen = refOpen[1];

    var current = STEPS[step];

    /* On every step change: navigate + apply highlight. */
    React.useEffect(function () {
      if (!open || !current) return;
      if (window.FS && window.FS.Router && current.route) {
        window.FS.Router.navigate(current.route);
      }
      applyHighlight(current.highlight);
    }, [step, open]);

    /* Cleanup highlights when the tour closes or unmounts. */
    React.useEffect(function () {
      return function () { clearAllHighlights(); };
    }, []);

    function next() {
      if (step >= STEPS.length - 1) close();
      else setStep(step + 1);
    }
    function prev() {
      if (step > 0) setStep(step - 1);
    }
    function close() {
      clearAllHighlights();
      setOpen(false);
      if (props.onClose) props.onClose();
    }

    if (!open || !current) return null;

    return React.createElement('div', {
      className:    'fs-demo-tour',
      role:         'dialog',
      'aria-label': 'Product tour',
    },
      React.createElement('div', { className: 'fs-demo-tour__counter' },
        'Step ' + (step + 1) + ' of ' + STEPS.length),
      React.createElement('div', { className: 'fs-demo-tour__title' }, current.title),
      React.createElement('div', { className: 'fs-demo-tour__text' },  current.text),
      React.createElement('div', { className: 'fs-demo-tour__actions' },
        React.createElement('button', {
          type:        'button',
          className:   'fs-demo-tour__btn fs-demo-tour__btn--ghost',
          onClick:     close,
          'aria-label': 'End tour',
        }, 'Skip'),
        step > 0 ? React.createElement('button', {
          type:      'button',
          className: 'fs-demo-tour__btn fs-demo-tour__btn--secondary',
          onClick:   prev,
        }, 'Prev') : null,
        React.createElement('button', {
          type:      'button',
          className: 'fs-demo-tour__btn fs-demo-tour__btn--primary',
          onClick:   next,
        }, step >= STEPS.length - 1 ? 'Done' : 'Next'),
      ),
    );
  }

  if (!window.FieldSight) window.FieldSight = {};
  window.FieldSight.DemoTour          = DemoTour;
  window.FieldSight.shouldRunDemoTour = shouldRunDemoTour;
})();
