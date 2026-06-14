/* ==========================================================================
   FieldSight HealthScore — Layer 5 composite (Sprint 9 Track C.2)
   --------------------------------------------------------------------------
   Combined-grade health badge: A / B / C / D, displayed as a coloured
   ring with the grade letter inside. Used by /portfolio · /regional
   · /executive to summarise project health at a glance.

   The grade is computed UPSTREAM in strategic-aggregator (so the
   composite is dumb — it just renders). Tones:
     A → success  (green)
     B → info     (blue)
     C → warning  (amber)
     D → danger   (red)

   Props:
     grade   'A' | 'B' | 'C' | 'D'
     size    'sm' | 'md' | 'lg'  (default 'md')
     label   optional caption rendered below the ring
     title   optional tooltip text

   Exported to: window.FieldSight.HealthScore
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  var TONES = {
    A: { bg: 'rgba(22, 163, 74, 0.15)',  ring: 'var(--color-success-500)', text: 'var(--color-success-700)' },
    B: { bg: 'rgba(37, 99, 235, 0.15)',  ring: 'var(--color-info-500)',    text: 'var(--color-info-700)'    },
    C: { bg: 'rgba(217, 119, 6, 0.15)',  ring: 'var(--color-warning-500)', text: 'var(--color-warning-700)' },
    D: { bg: 'rgba(220, 38, 38, 0.15)',  ring: 'var(--color-danger-500)',  text: 'var(--color-danger-700)'  },
  };

  var SIZES = {
    sm: { d: 28, font: 13 },
    md: { d: 40, font: 17 },
    lg: { d: 56, font: 22 },
  };

  function HealthScore(props) {
    var grade = (props.grade || 'A').toUpperCase();
    var size  = props.size || 'md';
    var label = props.label;
    var title = props.title;

    var tone = TONES[grade] || TONES.A;
    var sz   = SIZES[size]  || SIZES.md;

    return React.createElement('div', {
      className: 'fs-health-score fs-health-score--' + size,
      title:     title,
    },
      React.createElement('div', {
        className: 'fs-health-score__ring fs-health-score__ring--' + grade.toLowerCase(),
        style: {
          width:        sz.d + 'px',
          height:       sz.d + 'px',
          background:   tone.bg,
          borderColor:  tone.ring,
          color:        tone.text,
          fontSize:     sz.font + 'px',
        },
      }, grade),
      label ? React.createElement('div', { className: 'fs-health-score__label' },
        label) : null,
    );
  }

  if (!window.FieldSight) window.FieldSight = {};
  window.FieldSight.HealthScore = HealthScore;

})();
