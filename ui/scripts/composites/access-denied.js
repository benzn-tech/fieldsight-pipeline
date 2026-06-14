/* ==========================================================================
   FieldSight AccessDenied — Layer 5 composite (Sprint 2.9 / Phase I)
   --------------------------------------------------------------------------
   Empathetic 403 state for "the API said no". BACKEND-CONTEXT §8.4
   makes a point of NOT showing a generic toast — when a non-admin
   queries someone else's data, the API returns 403 with a structured
   error body and the UI should explain the missing access.

   Pages call this when an api/* response carries `_accessDenied: true`.

   Props:
     message     string — backend-provided reason (default sensible)
     scope       optional string — what was being accessed (e.g.
                 'this user' or 'this site')
     onBack      optional () => void

   Exported to:
     window.FieldSight.AccessDenied
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  function AccessDenied(props) {
    var Button = window.FieldSight.Button;
    var caller = (window.AuthMock && window.AuthMock.currentUser) || {};
    var sessionUser = window.FS && window.FS.session && window.FS.session.user;
    var role = (sessionUser && sessionUser.role) || caller.role || '';

    return React.createElement('div', { className: 'fs-access-denied' },
      React.createElement('div', { className: 'fs-access-denied__icon' }, '⛔'),
      React.createElement('div', { className: 'fs-access-denied__title' },
        'You don’t have access to ' + (props.scope || 'this')),
      React.createElement('div', { className: 'fs-access-denied__body' },
        props.message || 'Your role doesn’t permit reading this. Ask a project manager or admin if you need it.'),
      role ? React.createElement('div', { className: 'fs-access-denied__role' },
        'Signed in as · ' + role) : null,
      props.onBack ? React.createElement(Button, {
        size:     'sm',
        variant:  'secondary',
        onClick:  props.onBack,
      }, 'Back') : null,
    );
  }

  if (!window.FieldSight) window.FieldSight = {};
  window.FieldSight.AccessDenied = AccessDenied;
})();
