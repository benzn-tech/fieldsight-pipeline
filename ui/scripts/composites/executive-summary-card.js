/* ==========================================================================
   FieldSight ExecutiveSummaryCard — Layer 5 composite
   --------------------------------------------------------------------------
   Renders report.executive_summary as a bullet list inside a Card. The
   field is an ARRAY of strings since prompt_templates v3.0 (BACKEND-
   CONTEXT §5.1). We defensively also accept a single string and an empty
   array — empty just renders nothing.

   Props:
     bullets  string[] | string | null/undefined
     label    string — section title (default 'Executive summary')

   Exported to:
     window.FieldSight.ExecutiveSummaryCard
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  function normalise(input) {
    if (!input) return [];
    if (Array.isArray(input)) return input.filter(Boolean);
    return [String(input)];
  }

  function ExecutiveSummaryCard(props) {
    var Card = window.FieldSight.Card;
    var bullets = normalise(props.bullets);
    if (bullets.length === 0) return null;

    var label = props.label || 'Executive summary';

    return React.createElement(Card, {
      padding:   'md',
      className: 'fs-executive-summary',
    },
      React.createElement(Card.Body, null,
        React.createElement('div', {
          className: 'fs-executive-summary__label',
        }, label),
        React.createElement('ul', { className: 'fs-executive-summary__bullets' },
          bullets.map(function (b, i) {
            return React.createElement('li', {
              key:       i,
              className: 'fs-executive-summary__bullet',
            }, b);
          })
        ),
      ),
    );
  }

  if (!window.FieldSight) window.FieldSight = {};
  window.FieldSight.ExecutiveSummaryCard = ExecutiveSummaryCard;
})();
