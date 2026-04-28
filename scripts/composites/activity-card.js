/* ==========================================================================
   FieldSight ActivityCard — Layer 5 composite
   --------------------------------------------------------------------------
   Renders an activity event (e.g. PTT-transcribed snippet) as a
   clickable Card: avatar + full-wrap snippet + speaker · time meta line.

   Props:
     item       { id, speaker, snippet, timeAgo, channel, ... }
     onSelect   (item) => void — click handler

   Exported to:
     window.FieldSight.ActivityCard
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  function ActivityCard(props) {
    var Card   = window.FieldSight.Card;
    var Avatar = window.FieldSight.Avatar;

    var item     = props.item;
    var onSelect = props.onSelect;

    return React.createElement(Card, {
      padding: 'sm',
      onClick: onSelect ? function() { onSelect(item); } : undefined,
      className: 'fs-activity-card',
    },
      React.createElement(Card.Body, null,
        React.createElement('div', { className: 'fs-activity-card__row' },
          React.createElement(Avatar, { name: item.speaker, size: 'sm' }),
          React.createElement('div', { className: 'fs-activity-card__main' },
            React.createElement('div', { className: 'fs-activity-card__snippet' },
              item.snippet),
            React.createElement('div', { className: 'fs-activity-card__meta' },
              item.speaker + ' · ' + item.timeAgo),
          ),
        ),
      ),
    );
  }

  if (!window.FieldSight) window.FieldSight = {};
  window.FieldSight.ActivityCard = ActivityCard;
})();
