/* ==========================================================================
   FieldSight MorningBriefCard — Layer 5 composite
   --------------------------------------------------------------------------
   Renders the AI-generated overnight briefing as an elevated Card
   with a bullet list and a "Read full brief" link. Collapse is a
   *controlled* prop so parent state can drive it; defaults to a
   console.log stub when no callback is supplied (Sprint 2.1 wires
   the real toggle).

   Props:
     brief             { generatedAt, bullets: [string] }
     collapsed         boolean — hide body + footer when true
     onToggleCollapse  () => void — chevron click handler
     onOpenFull        () => void — "Read full brief" handler

   Exported to:
     window.FieldSight.MorningBriefCard
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  function MorningBriefCard(props) {
    var Card       = window.FieldSight.Card;
    var Button     = window.FieldSight.Button;
    var IconButton = window.FieldSight.IconButton;

    var brief            = props.brief;
    var collapsed        = !!props.collapsed;
    var onToggleCollapse = props.onToggleCollapse;
    var onOpenFull       = props.onOpenFull;

    function handleToggle() {
      if (onToggleCollapse) onToggleCollapse();
      else console.log('[Brief] toggle collapse — Sprint 2 wires this');
    }
    function handleOpen() {
      if (onOpenFull) onOpenFull();
      else console.log('[Brief] open full brief — Sprint 2 wires this');
    }

    var className = 'fs-morning-brief-card' +
      (collapsed ? ' fs-morning-brief-card--collapsed' : '');

    return React.createElement(Card, {
      variant: 'elevated',
      padding: 'md',
      className: className,
    },
      React.createElement(Card.Header, {
        title: 'Morning Brief',
        subtitle: 'Generated from overnight transcripts · ' + brief.generatedAt,
        actions: React.createElement(IconButton, {
          icon:      collapsed ? 'chevron-down' : 'chevron-up',
          ariaLabel: collapsed ? 'Expand brief'  : 'Collapse brief',
          size: 'sm',
          onClick: handleToggle,
        }),
      }),

      !collapsed ? React.createElement(Card.Body, null,
        React.createElement('ul', { className: 'fs-morning-brief-card__bullets' },
          brief.bullets.map(function(b, i) {
            return React.createElement('li', {
              key: i, className: 'fs-morning-brief-card__bullet',
            }, b);
          })
        ),
      ) : null,

      !collapsed ? React.createElement(Card.Footer, { align: 'start' },
        React.createElement(Button, {
          variant: 'tertiary', size: 'sm', rightIcon: 'arrow-right',
          onClick: handleOpen,
        }, 'Read full brief'),
      ) : null,
    );
  }

  if (!window.FieldSight) window.FieldSight = {};
  window.FieldSight.MorningBriefCard = MorningBriefCard;
})();
