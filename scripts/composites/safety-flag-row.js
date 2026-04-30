/* ==========================================================================
   FieldSight SafetyFlagRow — Layer 5 composite
   --------------------------------------------------------------------------
   One row in a topic's safety_flags list (BACKEND-CONTEXT §5.1):

     [risk badge]  observation
                   recommended_action

   Risk colour split (CLAUDE.md design system):
     high    → red    (danger tone)
     medium  → amber  (warning tone)
     low     → neutral

   Reused by site-wide safety_observations on the right detail too — that
   payload also has `location` + `who_raised`, surfaced as caption when
   present.

   Props:
     flag  { observation, risk_level, recommended_action,
             location?, who_raised? }
     dense  boolean — tighter padding when used in a list

   Exported to:
     window.FieldSight.SafetyFlagRow
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  var RISK_TONE = { high: 'danger', medium: 'warning', low: 'neutral' };

  function SafetyFlagRow(props) {
    var Badge = window.FieldSight.Badge;
    var flag  = props.flag || {};
    var risk  = (flag.risk_level || 'medium').toLowerCase();
    var tone  = RISK_TONE[risk] || 'neutral';

    var className = 'fs-safety-flag-row'
      + (props.dense ? ' fs-safety-flag-row--dense' : '')
      + ' fs-safety-flag-row--' + risk;

    var captionParts = [];
    if (flag.location)   captionParts.push(flag.location);
    if (flag.who_raised) captionParts.push('raised by ' + flag.who_raised);

    return React.createElement('div', { className: className },
      React.createElement(Badge, {
        tone:      tone,
        size:      'sm',
        variant:   'subtle',
        className: 'fs-safety-flag-row__risk',
      }, risk.charAt(0).toUpperCase() + risk.slice(1) + ' risk'),

      React.createElement('div', { className: 'fs-safety-flag-row__main' },
        React.createElement('div', { className: 'fs-safety-flag-row__obs' },
          flag.observation),
        captionParts.length > 0
          ? React.createElement('div', { className: 'fs-safety-flag-row__caption' },
              captionParts.join(' · '))
          : null,
        flag.recommended_action
          ? React.createElement('div', { className: 'fs-safety-flag-row__action' },
              React.createElement('span', {
                className: 'fs-safety-flag-row__action-label',
              }, 'Action · '),
              flag.recommended_action)
          : null,
      ),
    );
  }

  if (!window.FieldSight) window.FieldSight = {};
  window.FieldSight.SafetyFlagRow = SafetyFlagRow;
})();
