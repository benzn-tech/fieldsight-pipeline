/* ==========================================================================
   FieldSight MorningBriefCard — Layer 5 composite
   --------------------------------------------------------------------------
   Renders the AI-generated overnight briefing as an elevated Card with
   a bullet list. Collapse / expand is local state by default; pass
   both `collapsed` + `onToggleCollapse` to drive it from the parent.

   The CTA to open the full daily report is intentionally NOT inside
   this card — it's lifted to the Today page header
   (`.fs-today__view-report-cta` banner) so the action stands on its
   own and the brief stays a focused summary.

   Props:
     brief             { generatedAt, bullets: [string], date?, userFolder? }
     collapsed         boolean — controlled mode (with onToggleCollapse)
     defaultCollapsed  boolean — initial state when uncontrolled
     onToggleCollapse  () => void — chevron handler (controlled mode)

   Exported to:
     window.FieldSight.MorningBriefCard
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  function MorningBriefCard(props) {
    var Card       = window.FieldSight.Card;
    var IconButton = window.FieldSight.IconButton;

    var brief             = props.brief || { bullets: [] };
    var controlled        = typeof props.collapsed === 'boolean'
                         && typeof props.onToggleCollapse === 'function';

    var ref = React.useState(!!props.defaultCollapsed);
    var localCollapsed    = ref[0];
    var setLocalCollapsed = ref[1];

    var collapsed = controlled ? !!props.collapsed : localCollapsed;

    function handleToggle() {
      if (controlled) {
        props.onToggleCollapse();
      } else {
        setLocalCollapsed(function (c) { return !c; });
      }
    }

    var className = 'fs-morning-brief-card' +
      (collapsed ? ' fs-morning-brief-card--collapsed' : '');

    return React.createElement(Card, {
      variant:   'elevated',
      padding:   'md',
      className: className,
    },
      React.createElement(Card.Header, {
        title:    'Morning Brief',
        subtitle: 'Generated from overnight transcripts · ' + (brief.generatedAt || ''),
        actions:  React.createElement(IconButton, {
          icon:      collapsed ? 'chevron-down' : 'chevron-up',
          ariaLabel: collapsed ? 'Expand brief'  : 'Collapse brief',
          size:      'sm',
          onClick:   handleToggle,
        }),
      }),

      !collapsed ? React.createElement(Card.Body, null,
        React.createElement('ul', { className: 'fs-morning-brief-card__bullets' },
          (brief.bullets || []).map(function(b, i) {
            return React.createElement('li', {
              key: i, className: 'fs-morning-brief-card__bullet',
            }, b);
          })
        ),
      ) : null,
    );
  }

  if (!window.FieldSight) window.FieldSight = {};
  window.FieldSight.MorningBriefCard = MorningBriefCard;
})();
