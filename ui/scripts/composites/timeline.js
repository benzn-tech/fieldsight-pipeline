/* ==========================================================================
   FieldSight Timeline — Layer 5 composite
   --------------------------------------------------------------------------
   Generic vertical event log. A single 2px-wide guideline runs down
   the left; each entry is a label + actor · time meta line.

   Props:
     events   [{ label, actor, time }, ...]

   Exported to:
     window.FieldSight.Timeline
   ========================================================================== */

/* global React, window */

(function () {
  'use strict';

  function Timeline(props) {
    var events = props.events || [];
    if (events.length === 0) return null;

    return React.createElement('div', { className: 'fs-timeline' },
      events.map(function(e, i) {
        return React.createElement('div', {
          key: i, className: 'fs-timeline__entry',
        },
          React.createElement('div', { className: 'fs-timeline__label' },
            e.label),
          React.createElement('div', { className: 'fs-timeline__meta' },
            e.actor + ' · ' + e.time),
        );
      }),
    );
  }

  if (!window.FieldSight) window.FieldSight = {};
  window.FieldSight.Timeline = Timeline;
})();
