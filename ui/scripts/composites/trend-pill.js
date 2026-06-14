/* ==========================================================================
   FieldSight TrendPill — Layer 5 composite (Sprint 9 Track A.2)
   --------------------------------------------------------------------------
   Compact "↑ 5 vs last week" badge for KPI cards + chart legends.
   Auto-tones based on direction + the metric's polarity (rising
   safety incidents = bad → danger; rising completion = good →
   success). Caller declares whether higher-is-good via `polarity`.

   Props:
     delta     number — signed change (this period − last period)
     unit      string — 'issues' | '%' | 'flags' | …  (default '')
     polarity  'higher_better' | 'lower_better' (default 'lower_better'
                — sane for incident counts)
     compare   string — caption suffix, e.g. 'vs last week'
                (default 'vs last week')
     hideZero  boolean — render '–' if delta is 0 (default false)

   Tones:
     polarity = lower_better:
        delta > 0 → danger    (more issues = bad)
        delta < 0 → success   (fewer issues = good)
        delta = 0 → neutral
     polarity = higher_better:
        delta > 0 → success
        delta < 0 → danger
        delta = 0 → neutral

   Exported to: window.FieldSight.TrendPill
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  function pickTone(delta, polarity) {
    if (delta === 0) return 'neutral';
    var goodDirection = polarity === 'higher_better' ? 1 : -1;
    return ((delta > 0 ? 1 : -1) === goodDirection) ? 'success' : 'danger';
  }

  function TrendPill(props) {
    var delta    = typeof props.delta === 'number' ? props.delta : 0;
    var unit     = props.unit     || '';
    var polarity = props.polarity || 'lower_better';
    var compare  = props.compare != null ? props.compare : 'vs last week';
    var hideZero = !!props.hideZero;

    if (delta === 0 && hideZero) {
      return React.createElement('span', {
        className: 'fs-trend-pill fs-trend-pill--neutral',
      }, '–');
    }

    var tone = pickTone(delta, polarity);
    var arrow = delta > 0 ? '↑' : delta < 0 ? '↓' : '·';
    var magnitude = Math.abs(delta).toLocaleString();

    return React.createElement('span', {
      className: 'fs-trend-pill fs-trend-pill--' + tone,
      title:     'Change from previous period',
    },
      React.createElement('span', { className: 'fs-trend-pill__arrow' }, arrow),
      ' ',
      magnitude + (unit ? ' ' + unit : ''),
      compare ? React.createElement('span', { className: 'fs-trend-pill__compare' },
        ' ' + compare) : null,
    );
  }

  if (!window.FieldSight) window.FieldSight = {};
  window.FieldSight.TrendPill = TrendPill;

})();
