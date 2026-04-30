/* ==========================================================================
   FieldSight UrgentCard — Layer 5 composite
   --------------------------------------------------------------------------
   Renders an urgent item as a clickable Card. Sprint 3 (P-01) extended
   the layout so the safety context lives entirely in the middle column
   — clicking through to the right detail is for context, not for
   discovering the risk level or what to do about it:

     Header   : title + risk pill (or legacy badgeLabel for non-safety)
     Body     : observation (item.body)
     Action   : "Action · {recommendedAction}"  (only when present)
     Caption  : optional location line for safety_observations

   Props:
     item       { id, title, body, badgeTone, badgeLabel,
                  riskLevel?, recommendedAction?, location?, ... }
     onSelect   (item) => void — click handler

   Exported to:
     window.FieldSight.UrgentCard
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  var RISK_TONE = { high: 'danger', medium: 'warning', low: 'neutral' };

  function UrgentCard(props) {
    var Card  = window.FieldSight.Card;
    var Badge = window.FieldSight.Badge;

    var item     = props.item;
    var onSelect = props.onSelect;

    /* Prefer the structured risk pill when available; fall back to the
       legacy badgeLabel for non-safety urgent items. */
    var hasRisk = !!item.riskLevel;
    var riskTone  = hasRisk ? (RISK_TONE[item.riskLevel] || 'neutral') : null;
    var riskLabel = hasRisk
      ? (item.riskLevel.charAt(0).toUpperCase() + item.riskLevel.slice(1) + ' risk')
      : null;

    var headerBadge = hasRisk
      ? React.createElement(Badge, {
          tone: riskTone, size: 'sm', prefixDot: true,
        }, riskLabel)
      : React.createElement(Badge, {
          tone: item.badgeTone, size: 'sm', prefixDot: true,
        }, item.badgeLabel);

    return React.createElement(Card, {
      variant: 'default',
      padding: 'sm',
      onClick: onSelect ? function() { onSelect(item); } : undefined,
      className: 'fs-urgent-card' + (hasRisk ? ' fs-urgent-card--risk-' + item.riskLevel : ''),
    },
      React.createElement(Card.Header, {
        title:   item.title,
        actions: headerBadge,
      }),
      React.createElement(Card.Body, null,
        item.body ? React.createElement('div', {
          className: 'fs-urgent-card__body',
        }, item.body) : null,

        item.recommendedAction ? React.createElement('div', {
          className: 'fs-urgent-card__action',
        },
          React.createElement('span', {
            className: 'fs-urgent-card__action-label',
          }, 'Action · '),
          item.recommendedAction,
        ) : null,

        item.location ? React.createElement('div', {
          className: 'fs-urgent-card__caption',
        }, item.location) : null,
      ),
    );
  }

  if (!window.FieldSight) window.FieldSight = {};
  window.FieldSight.UrgentCard = UrgentCard;
})();
