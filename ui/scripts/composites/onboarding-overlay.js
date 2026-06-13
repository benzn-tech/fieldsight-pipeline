/* ==========================================================================
   FieldSight OnboardingOverlay — Sprint 8.11.1
   --------------------------------------------------------------------------
   First-run welcome overlay. Three-step centred card; sets
   localStorage['fs.onboarded'] = '1' on completion or skip so it doesn't
   re-appear. Also resettable from /settings (Sprint 7).

   Activation:
     - AppShell mounts this when localStorage['fs.onboarded'] !== '1'
     - Or when ?onboarding=1 is in the URL (dev override)

   Reduced-motion: relies on the underlying ModalOverlay; no per-step slide
   animations introduced here.

   Exported to: window.FieldSight.OnboardingOverlay
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  function setOnboarded() {
    try { localStorage.setItem('fs.onboarded', '1'); } catch (_) {}
  }

  /* Friendly per-role description for step 2. */
  function roleMessage(user) {
    var role = (user && user.role) || 'worker';
    var byRole = {
      site_manager:   'You\'ll see your site\'s reports, tasks, and safety flags front-and-centre.',
      hse_manager:    'Safety observations and high-risk flags get top billing across every site you manage.',
      quality_manager:'Quality items, compliance follow-ups, and inspection deadlines drive your view.',
      pm:             'Programme schedule, cross-site rollups, and team allocations are your daily focus.',
      gm:             'Strategic dashboards summarise programme health and safety posture across the portfolio.',
      executive:      'Executive summary cards highlight what changed across all sites today.',
      admin:          'You have full access — every site, every page, plus team and settings administration.',
      worker:         'Your assigned tasks, the day\'s safety brief, and the timeline of recent updates are right here.',
      viewer:         'Read-only access to today\'s reports, timeline, and safety summaries.',
      foreman:        'Crew assignments, day-to-day tasks, and morning-brief actions live on your Today page.',
    };
    return byRole[role] || byRole.worker;
  }

  function OnboardingOverlay(props) {
    var Modal = window.FieldSight && window.FieldSight.ModalOverlay;
    var Button = window.FieldSight && window.FieldSight.Button;
    if (!Modal) return null;

    var refStep = React.useState(0);
    var step    = refStep[0];
    var setStep = refStep[1];

    function finish(targetRoute) {
      setOnboarded();
      if (targetRoute && window.FS && window.FS.Router) {
        window.FS.Router.navigate(targetRoute);
      }
      if (props.onClose) props.onClose();
    }

    function skip() {
      setOnboarded();
      if (props.onClose) props.onClose();
    }

    var steps = [
      {
        title: 'Welcome to FieldSight',
        body:  'FieldSight turns your daily site recordings into structured reports, tasks, and safety insights — all in one place.',
        primary: { label: 'Next →', action: function () { setStep(1); } },
      },
      {
        title: 'Tailored to your role',
        body:  roleMessage(props.user),
        primary: { label: 'Next →', action: function () { setStep(2); } },
      },
      {
        title: 'Start with Today',
        body:  'Today is the home base — your morning brief, urgent items, and on-site headcount. You can always come back to it from the sidebar.',
        primary: { label: 'Open Today →', action: function () { finish('/today'); } },
      },
    ];

    var s = steps[step];

    return React.createElement(Modal, {
      open:    true,
      onClose: skip,
      title:   s.title,
      size:    'sm',
      closeOnBackdrop: false,
    },
      React.createElement('div', { className: 'fs-onboarding' },
        React.createElement('div', { className: 'fs-onboarding__progress', 'aria-hidden': true },
          steps.map(function (_, i) {
            return React.createElement('span', {
              key:       i,
              className: 'fs-onboarding__dot' + (i === step ? ' fs-onboarding__dot--active' : ''),
            });
          }),
        ),
        React.createElement('p', { className: 'fs-onboarding__body' }, s.body),
        React.createElement('div', { className: 'fs-onboarding__actions' },
          React.createElement('button', {
            type:      'button',
            className: 'fs-onboarding__skip',
            onClick:   skip,
          }, 'Skip'),
          Button
            ? React.createElement(Button, {
                size:    'sm',
                variant: 'primary',
                onClick: s.primary.action,
              }, s.primary.label)
            : React.createElement('button', {
                type:      'button',
                className: 'fs-onboarding__primary',
                onClick:   s.primary.action,
              }, s.primary.label),
        ),
      ),
    );
  }

  if (!window.FieldSight) window.FieldSight = {};
  window.FieldSight.OnboardingOverlay = OnboardingOverlay;
})();
