/* ==========================================================================
   FieldSight UrgentCard — Layer 5 composite
   --------------------------------------------------------------------------
   Renders an urgent item as a clickable Card with a tone-coded badge
   in the header (e.g. "Overdue by 2h" / "Action by 14:00") and a
   one-line body explanation.

   Props:
     item       { id, title, body, badgeTone, badgeLabel, ... }
     onSelect   (item) => void — click handler

   Exported to:
     window.FieldSight.UrgentCard
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  function UrgentCard(props) {
    var Card  = window.FieldSight.Card;
    var Badge = window.FieldSight.Badge;

    var item     = props.item;
    var onSelect = props.onSelect;

    return React.createElement(Card, {
      variant: 'default',
      padding: 'sm',
      onClick: onSelect ? function() { onSelect(item); } : undefined,
      className: 'fs-urgent-card',
    },
      React.createElement(Card.Header, {
        title: item.title,
        actions: React.createElement(Badge, {
          tone: item.badgeTone,
          size: 'sm',
          prefixDot: true,
        }, item.badgeLabel),
      }),
      React.createElement(Card.Body, null,
        React.createElement('div', { className: 'fs-urgent-card__body' },
          item.body),
      ),
    );
  }

  if (!window.FieldSight) window.FieldSight = {};
  window.FieldSight.UrgentCard = UrgentCard;
})();
