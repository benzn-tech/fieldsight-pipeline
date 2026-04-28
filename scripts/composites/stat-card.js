/* ==========================================================================
   FieldSight StatCard — Layer 5 composite
   --------------------------------------------------------------------------
   Single-value KPI tile: a large tabular-nums number, an uppercase
   label, and an optional trend indicator (↑ +12% / ↓ -3%).

   Props:
     value    string | number — the headline value
     label    string — caption underneath
     trend    optional { direction: 'up' | 'down', delta: string }
     tone     optional 'neutral' | 'accent' | 'success' | 'warning' | 'danger'
              colours the value

   Exported to:
     window.FieldSight.StatCard
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  function StatCard(props) {
    var value = props.value;
    var label = props.label;
    var trend = props.trend;
    var tone  = props.tone || 'neutral';

    var className = 'fs-stat-card fs-stat-card--tone-' + tone;

    return React.createElement('div', { className: className },
      React.createElement('div', { className: 'fs-stat-card__value' }, value),
      React.createElement('div', { className: 'fs-stat-card__label' }, label),
      trend ? React.createElement('div', {
        className: 'fs-stat-card__trend fs-stat-card__trend--' + trend.direction,
      },
        (trend.direction === 'up' ? '↑ ' : '↓ ') + trend.delta,
      ) : null,
    );
  }

  if (!window.FieldSight) window.FieldSight = {};
  window.FieldSight.StatCard = StatCard;
})();
