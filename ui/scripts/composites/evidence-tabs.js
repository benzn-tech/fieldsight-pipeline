/* ==========================================================================
   FieldSight EvidenceTabs — Layer 5 composite (Sprint 4.3)
   --------------------------------------------------------------------------
   Tab strip for the /evidence page. Mirrors the visual rhythm of
   TopicDetail's tab strip (Phase B/C) but lives at page level rather
   than inside a card.

   Each tab carries an optional `count` (rendered as a small pill
   when present, hidden when null/undefined). Counts come from the
   page's lazy fetches — initial render shows tabs without counts;
   they fill in once the underlying tab fetches resolve.

   Props:
     tabs       [{ key, label, count? }]
     active     string — active key
     onChange   (key) => void

   Exported to:
     window.FieldSight.EvidenceTabs
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  function EvidenceTabs(props) {
    var tabs   = props.tabs   || [];
    var active = props.active || (tabs[0] && tabs[0].key);

    return React.createElement('div', {
      className: 'fs-evidence-tabs',
      role:      'tablist',
    },
      tabs.map(function (t) {
        var isActive = t.key === active;
        return React.createElement('button', {
          key:           t.key,
          type:          'button',
          role:          'tab',
          'aria-selected': isActive,
          className:     'fs-evidence-tabs__tab' + (isActive ? ' fs-evidence-tabs__tab--active' : ''),
          onClick:       function () { if (props.onChange) props.onChange(t.key); },
        },
          React.createElement('span', { className: 'fs-evidence-tabs__label' },
            t.label),
          (t.count != null) ? React.createElement('span', {
            className: 'fs-evidence-tabs__count',
          }, t.count) : null,
        );
      }),
    );
  }

  if (!window.FieldSight) window.FieldSight = {};
  window.FieldSight.EvidenceTabs = EvidenceTabs;
})();
