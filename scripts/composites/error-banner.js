/* ==========================================================================
   FieldSight · ErrorBanner — Sprint 8.0.3 / 8.7.1
   --------------------------------------------------------------------------
   Reusable error state component for page middle-columns and right-detail
   panels. Replaces plain-text error strings throughout the page providers.

   Props:
     message   string          — human-readable error description
     retryable bool            — show Retry button (default true)
     onRetry   function        — called when user clicks Retry
     mini      bool            — compact inline variant (right-detail panels)

   CSS: .fs-error-banner  in styles/composites.css

   Exported to: window.FieldSight.ErrorBanner
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  function ErrorBanner(props) {
    var message   = props.message   || 'Something went wrong.';
    var retryable = props.retryable !== false;  /* default true */
    var mini      = !!props.mini;

    var cn = 'fs-error-banner' + (mini ? ' fs-error-banner--mini' : '');

    var icon = React.createElement('span', {
      className: 'fs-error-banner__icon',
      'aria-hidden': 'true',
    }, '⚠');

    var body = React.createElement('span', { className: 'fs-error-banner__message' },
      message);

    var retryBtn = (retryable && props.onRetry)
      ? React.createElement('button', {
          type:      'button',
          className: 'fs-error-banner__retry',
          onClick:   props.onRetry,
        }, 'Retry')
      : null;

    return React.createElement('div', {
      className: cn,
      role:      'alert',
    }, icon, body, retryBtn);
  }

  if (!window.FieldSight) window.FieldSight = {};
  window.FieldSight.ErrorBanner = ErrorBanner;

})();
