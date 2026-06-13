/* ==========================================================================
   FieldSight OnSiteCard — Layer 5 composite
   --------------------------------------------------------------------------
   Shows a stacked AvatarGroup of people currently on site plus a
   trailing "N on site" count.

   Props:
     people   [{ id, name }, ...]
     max      number — AvatarGroup overflow threshold (default 5)

   Exported to:
     window.FieldSight.OnSiteCard
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  function OnSiteCard(props) {
    var Card        = window.FieldSight.Card;
    var Avatar      = window.FieldSight.Avatar;
    var AvatarGroup = window.FieldSight.AvatarGroup;

    var people = props.people || [];
    var max    = props.max || 5;

    return React.createElement(Card, {
      padding: 'md', className: 'fs-on-site-card',
    },
      React.createElement(Card.Body, null,
        React.createElement('div', { className: 'fs-on-site-card__row' },
          React.createElement(AvatarGroup, { size: 'md', max: max },
            people.map(function(p) {
              return React.createElement(Avatar, { key: p.id, name: p.name });
            })
          ),
          React.createElement('span', { className: 'fs-on-site-card__count' },
            people.length + ' on site'),
        ),
      ),
    );
  }

  if (!window.FieldSight) window.FieldSight = {};
  window.FieldSight.OnSiteCard = OnSiteCard;
})();
